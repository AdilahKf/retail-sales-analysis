#!/usr/bin/env python3
"""
clean_online_retail.py
======================
Cleaning pipeline untuk dataset Online Retail II (UCI / Kaggle) -> siap diimpor ke Power BI.

Cara pakai:
    python clean_online_retail.py --input online_retail_II.csv --output ./clean_output

Kebutuhan:
    pip install pandas numpy

Output (di folder --output):
    fact_sales.csv          transaksi penjualan valid (1 baris = 1 line item)
    fact_returns.csv        retur / pembatalan (invoice berawalan "C")
    non_product_items.csv   postage, biaya, adjustment, voucher, test (bukan produk)
    rejected_rows.csv       baris yang dibuang + alasannya (audit trail)
    dim_date.csv            kalender lengkap
    dim_product.csv         1 baris per StockCode (deskripsi sudah distandarkan)
    dim_customer.csv        1 baris per pelanggan (first/last purchase, cohort month)
    dim_country.csv         1 baris per negara
    data_quality_log.csv    log setiap langkah cleaning (baris sebelum/sesudah, dampak revenue)
    data_quality_profile.csv  profil masalah data mentah
    data_quality_report.md  ringkasan yang bisa langsung dibaca / dilampirkan di README

Prinsip:
    - Tidak ada baris yang hilang tanpa jejak: setiap baris mentah masuk tepat ke satu tabel output
      (fact_sales / fact_returns / non_product_items / rejected_rows). Ada rekonsiliasi di akhir.
    - Setiap aturan dicatat di data quality log beserta dampaknya pada jumlah baris dan nilai (Quantity x Price).
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------------------
# Konfigurasi aturan bisnis
# --------------------------------------------------------------------------------------

# StockCode yang BUKAN produk fisik. Kategori dipakai di tabel non_product_items.
NON_PRODUCT_CATEGORIES = {
    "POST": "Shipping",
    "DOT": "Shipping",
    "C2": "Shipping",
    "C3": "Shipping",
    "M": "Manual Entry",
    "D": "Discount",
    "S": "Samples",
    "BANK CHARGES": "Fees",
    "AMAZONFEE": "Fees",
    "CRUK": "Fees",
    "ADJUST": "Adjustment",
    "ADJUST2": "Adjustment",
    "B": "Adjustment",  # "Adjust bad debt"
    "PADS": "Other",
    "GIFT": "Gift Voucher",
}
NON_PRODUCT_PREFIXES = {"GIFT_": "Gift Voucher", "TEST": "Test"}

# Kuantil untuk menandai (bukan membuang) transaksi ekstrem
OUTLIER_QUANTILE = 0.999

RAW_COLUMN_MAP = {
    "Invoice": "InvoiceNo",
    "InvoiceNo": "InvoiceNo",
    "StockCode": "StockCode",
    "Description": "Description",
    "Quantity": "Quantity",
    "InvoiceDate": "InvoiceDate",
    "Price": "UnitPrice",
    "UnitPrice": "UnitPrice",
    "Customer ID": "CustomerID",
    "CustomerID": "CustomerID",
    "Country": "Country",
}


# --------------------------------------------------------------------------------------
# Utilitas
# --------------------------------------------------------------------------------------
def line_value(d: pd.DataFrame) -> float:
    """Nilai mentah Quantity x UnitPrice (bisa negatif untuk retur)."""
    return float((d["Quantity"] * d["UnitPrice"]).sum())


class QualityLog:
    """Mencatat setiap langkah cleaning."""

    def __init__(self, raw_rows: int):
        self.raw_rows = raw_rows
        self.rows: list[dict] = []

    def add(self, step: str, rule: str, action: str, rows_before: int, affected: int,
            rows_after: int, value_impact: float = 0.0, note: str = "") -> None:
        self.rows.append({
            "step_no": len(self.rows) + 1,
            "step": step,
            "rule": rule,
            "action": action,
            "rows_before": rows_before,
            "rows_affected": affected,
            "rows_after": rows_after,
            "pct_of_raw_rows": round(affected / self.raw_rows * 100, 3) if self.raw_rows else 0,
            "value_impact": round(value_impact, 2),
            "note": note,
        })
        print(f"  [{len(self.rows):02d}] {step:<34} {action:<22} {affected:>9,} baris")

    def to_df(self) -> pd.DataFrame:
        return pd.DataFrame(self.rows)


def read_raw(path: Path) -> pd.DataFrame:
    """Baca CSV mentah. Coba UTF-8 dulu, fallback ke cp1252."""
    dtype = {"Invoice": str, "InvoiceNo": str, "StockCode": str, "Description": str, "Country": str}
    for enc in ("utf-8", "cp1252"):
        try:
            df = pd.read_csv(path, dtype=dtype, encoding=enc)
            print(f"  File dibaca dengan encoding {enc}")
            return df
        except UnicodeDecodeError:
            continue
    raise SystemExit("Gagal membaca file: encoding tidak dikenali.")


def classify_non_product(stock: pd.Series) -> pd.Series:
    """Kembalikan kategori non-produk (NaN kalau produk biasa)."""
    cat = stock.map(NON_PRODUCT_CATEGORIES)
    for prefix, name in NON_PRODUCT_PREFIXES.items():
        cat = cat.where(~stock.str.startswith(prefix), name)
    return cat


def clean_description(s: pd.Series) -> pd.Series:
    return (s.astype("string").str.upper().str.replace(r"\s+", " ", regex=True).str.strip()
            .replace("", pd.NA))


def md_table(df: pd.DataFrame) -> str:
    cols = list(df.columns)
    out = ["| " + " | ".join(cols) + " |", "|" + "|".join(["---"] * len(cols)) + "|"]
    for _, r in df.iterrows():
        out.append("| " + " | ".join(f"{v:,}" if isinstance(v, (int, np.integer)) else
                                     f"{v:,.2f}" if isinstance(v, (float, np.floating)) else str(v)
                                     for v in r) + " |")
    return "\n".join(out)


# --------------------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------------------
def run(input_path: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n== Membaca {input_path} ==")
    raw = read_raw(input_path)
    raw = raw.rename(columns={c: RAW_COLUMN_MAP[c] for c in raw.columns if c in RAW_COLUMN_MAP})
    expected = {"InvoiceNo", "StockCode", "Description", "Quantity", "InvoiceDate",
                "UnitPrice", "CustomerID", "Country"}
    missing_cols = expected - set(raw.columns)
    if missing_cols:
        raise SystemExit(f"Kolom tidak ditemukan: {sorted(missing_cols)}")
    raw = raw[["InvoiceNo", "StockCode", "Description", "Quantity", "InvoiceDate",
               "UnitPrice", "CustomerID", "Country"]]

    raw_rows = len(raw)
    log = QualityLog(raw_rows)
    rejects: list[pd.DataFrame] = []

    # ---- Standarisasi tipe data (belum ada baris yang dibuang) ----
    for col in ("InvoiceNo", "StockCode", "Country"):
        raw[col] = raw[col].astype("string").str.strip()
    raw["StockCode"] = raw["StockCode"].str.upper()
    raw["Description"] = raw["Description"].astype("string").str.strip().replace("", pd.NA)
    raw["Quantity"] = pd.to_numeric(raw["Quantity"], errors="coerce")
    raw["UnitPrice"] = pd.to_numeric(raw["UnitPrice"], errors="coerce")
    raw["InvoiceDate"] = pd.to_datetime(raw["InvoiceDate"], errors="coerce")
    raw["CustomerID"] = pd.to_numeric(raw["CustomerID"], errors="coerce").astype("Int64")
    raw["RawRowNo"] = np.arange(2, raw_rows + 2)  # nomor baris di file sumber (baris 1 = header)

    # ---- Profil data mentah ----
    raw_value = line_value(raw.dropna(subset=["Quantity", "UnitPrice"]))
    is_cancel = raw["InvoiceNo"].str.upper().str.startswith("C").fillna(False)
    profile = pd.DataFrame([
        ("Total baris", raw_rows),
        ("Rentang tanggal (awal)", str(raw["InvoiceDate"].min())),
        ("Rentang tanggal (akhir)", str(raw["InvoiceDate"].max())),
        ("InvoiceDate tidak valid", int(raw["InvoiceDate"].isna().sum())),
        ("CustomerID kosong", int(raw["CustomerID"].isna().sum())),
        ("Description kosong", int(raw["Description"].isna().sum())),
        ("Baris duplikat (identik penuh)", int(raw.drop(columns="RawRowNo").duplicated().sum())),
        ("Invoice batal (awalan C)", int(is_cancel.sum())),
        ("Quantity <= 0", int((raw["Quantity"] <= 0).sum())),
        ("UnitPrice <= 0", int((raw["UnitPrice"] <= 0).sum())),
        ("Baris StockCode non-produk", int(classify_non_product(raw["StockCode"]).notna().sum())),
        ("Pelanggan unik", int(raw["CustomerID"].nunique())),
        ("Negara unik", int(raw["Country"].nunique())),
        ("Nilai mentah total (Qty x Price)", round(raw_value, 2)),
    ], columns=["check", "value"])

    print("\n== Langkah cleaning ==")

    work = raw.copy()

    def reject(mask: pd.Series, reason: str, step: str, rule: str, note: str = "") -> None:
        nonlocal work
        part = work[mask]
        rejects.append(part.assign(RejectReason=reason))
        before = len(work)
        work = work[~mask]
        log.add(step, rule, "Dibuang -> rejected", before, len(part), len(work),
                line_value(part.dropna(subset=["Quantity", "UnitPrice"])), note)

    # 1. Tanggal tidak valid atau Quantity/Price tidak terbaca
    bad = work["InvoiceDate"].isna() | work["Quantity"].isna() | work["UnitPrice"].isna()
    reject(bad, "INVALID_DATE_OR_NUMBER", "Validasi tipe data",
           "InvoiceDate / Quantity / UnitPrice tidak bisa diparse")

    # 2. Duplikat identik penuh
    dup = work.drop(columns="RawRowNo").duplicated(keep="first")
    reject(dup, "DUPLICATE_ROW", "Hapus duplikat",
           "Semua kolom identik dengan baris sebelumnya (keep first)",
           "Asumsi: duplikat penuh = kesalahan input/ekspor. Catatan: bisa saja sebagian pembelian ganda yang sah.")

    # 3. Non-produk -> tabel terpisah
    work["NonProductCategory"] = classify_non_product(work["StockCode"])
    np_mask = work["NonProductCategory"].notna()
    non_product = work[np_mask].copy()
    before = len(work)
    work = work[~np_mask].drop(columns="NonProductCategory")
    log.add("Pisahkan non-produk",
            "StockCode di daftar non-produk (POST, DOT, M, D, S, ADJUST, AMAZONFEE, GIFT_*, TEST*, dst)",
            "Dipindah -> non_product_items", before, len(non_product), len(work), line_value(non_product),
            "Tidak boleh masuk revenue produk / ranking produk. Gift voucher dipisah karena bukan barang.")

    # 4. Retur / pembatalan -> tabel terpisah
    is_c = work["InvoiceNo"].str.upper().str.startswith("C").fillna(False)
    returns = work[is_c].copy()
    before = len(work)
    work = work[~is_c]
    log.add("Pisahkan retur/pembatalan", "InvoiceNo diawali 'C'", "Dipindah -> fact_returns",
            before, len(returns), len(work), line_value(returns),
            "Retur dihitung terpisah agar Net Revenue = Revenue - Returns bisa dihitung eksplisit.")

    # 4b. Validasi baris retur (harus Quantity<0 dan Price>0)
    inv_ret = (returns["Quantity"] >= 0) | (returns["UnitPrice"] <= 0)
    if inv_ret.any():
        part = returns[inv_ret]
        rejects.append(part.assign(RejectReason="INVALID_RETURN_ROW"))
        b = len(returns)
        returns = returns[~inv_ret]
        log.add("Validasi baris retur", "Retur harus Quantity<0 dan UnitPrice>0", "Dibuang -> rejected",
                b, len(part), len(returns), line_value(part))

    # 5. Price <= 0 pada baris penjualan
    reject(work["UnitPrice"] <= 0, "ZERO_OR_NEGATIVE_PRICE", "Hapus harga <= 0",
           "UnitPrice <= 0 pada invoice non-batal",
           "Umumnya stok hilang/rusak/barang gratis; tidak menghasilkan revenue.")

    # 6. Quantity <= 0 pada baris penjualan (bukan invoice batal)
    reject(work["Quantity"] <= 0, "NONPOSITIVE_QTY_NOT_CANCELLATION", "Hapus quantity <= 0",
           "Quantity <= 0 pada invoice non-batal (setelah langkah harga <= 0)",
           "Umumnya penyesuaian stok (damaged/lost); bukan penjualan. Pada data ini hasilnya 0 karena "
           "semua baris seperti itu sudah tertangkap di langkah harga <= 0.")

    sales = work.copy()

    # 7. Standarisasi deskripsi produk: 1 StockCode = 1 deskripsi (yang paling sering)
    sales["Description"] = clean_description(sales["Description"])
    returns["Description"] = clean_description(returns["Description"])
    all_prod = pd.concat([sales[["StockCode", "Description"]], returns[["StockCode", "Description"]]])
    cnt = (all_prod.dropna().groupby(["StockCode", "Description"]).size().reset_index(name="n")
           .sort_values(["StockCode", "n", "Description"], ascending=[True, False, True]))
    n_variants = int((cnt.groupby("StockCode").size() > 1).sum())
    best_desc = cnt.drop_duplicates("StockCode").set_index("StockCode")["Description"]
    miss_before = int(sales["Description"].isna().sum() + returns["Description"].isna().sum())
    changed = int((sales["Description"] != sales["StockCode"].map(best_desc)).fillna(False).sum())
    sales["Description"] = sales["StockCode"].map(best_desc).fillna("UNKNOWN PRODUCT")
    returns["Description"] = returns["StockCode"].map(best_desc).fillna("UNKNOWN PRODUCT")
    log.add("Standarisasi deskripsi produk",
            "Uppercase, rapikan spasi, 1 deskripsi (paling sering) per StockCode; kosong diisi dari StockCode yang sama",
            "Diubah (tanpa hapus)", len(sales), changed, len(sales), 0,
            f"{n_variants:,} StockCode punya >1 variasi deskripsi; {miss_before:,} deskripsi kosong tersisa sebelum pengisian.")

    # 8. Kolom turunan
    for d in (sales, returns):
        d["InvoiceDateOnly"] = d["InvoiceDate"].dt.normalize()
        d["HasCustomer"] = d["CustomerID"].notna()
    sales["LineRevenue"] = (sales["Quantity"] * sales["UnitPrice"]).round(4)
    returns["ReturnQty"] = -returns["Quantity"]
    returns["ReturnValue"] = (-returns["Quantity"] * returns["UnitPrice"]).round(4)
    log.add("Hitung LineRevenue", "LineRevenue = Quantity x UnitPrice; ReturnValue = -Quantity x UnitPrice",
            "Kolom baru", len(sales), len(sales), len(sales), 0)

    no_cust = int((~sales["HasCustomer"]).sum())
    log.add("Tandai CustomerID kosong", "HasCustomer = False bila CustomerID kosong", "Ditandai (tanpa hapus)",
            len(sales), no_cust, len(sales), float(sales.loc[~sales["HasCustomer"], "LineRevenue"].sum()),
            "Tetap dipakai untuk analisis penjualan; WAJIB difilter HasCustomer=True untuk RFM, cohort, retensi.")

    # 9. Flag outlier (tidak dibuang)
    q_thr = sales["Quantity"].quantile(OUTLIER_QUANTILE)
    r_thr = sales["LineRevenue"].quantile(OUTLIER_QUANTILE)
    sales["IsOutlier"] = (sales["Quantity"] >= q_thr) | (sales["LineRevenue"] >= r_thr)
    log.add("Tandai outlier", f"Quantity >= {q_thr:,.0f} atau LineRevenue >= {r_thr:,.2f} (persentil {OUTLIER_QUANTILE:.1%})",
            "Ditandai (tanpa hapus)", len(sales), int(sales["IsOutlier"].sum()), len(sales),
            float(sales.loc[sales["IsOutlier"], "LineRevenue"].sum()),
            "Order grosir besar bisa sah. Gunakan slicer IsOutlier untuk membandingkan dengan/tanpa outlier.")

    # ---- Rekonsiliasi ----
    n_rej = sum(len(r) for r in rejects)
    total_out = len(sales) + len(returns) + len(non_product) + n_rej
    assert total_out == raw_rows, f"Rekonsiliasi baris gagal: {total_out} != {raw_rows}"
    val_out = (line_value(sales) + line_value(returns) + line_value(non_product)
               + sum(line_value(r.dropna(subset=['Quantity', 'UnitPrice'])) for r in rejects)
               + 0.0)
    # baris INVALID_DATE_OR_NUMBER tanpa angka tidak punya nilai; selisih harus ~0
    assert abs(val_out - raw_value) < 1.0, f"Rekonsiliasi nilai gagal: {val_out:,.2f} vs {raw_value:,.2f}"
    log.add("Rekonsiliasi akhir",
            "fact_sales + fact_returns + non_product_items + rejected_rows = baris mentah",
            "Cek (lulus)", raw_rows, total_out, total_out, val_out - raw_value,
            f"Nilai mentah {raw_value:,.2f} = jumlah semua tabel output.")

    # ---- Dimensi ----
    all_dates = pd.concat([sales["InvoiceDateOnly"], returns["InvoiceDateOnly"]])
    d_min, d_max = all_dates.min(), all_dates.max()
    cal = pd.DataFrame({"Date": pd.date_range(d_min.replace(day=1), d_max + pd.offsets.MonthEnd(0))})
    cal["Year"] = cal["Date"].dt.year
    cal["Quarter"] = "Q" + cal["Date"].dt.quarter.astype(str)
    cal["YearQuarter"] = cal["Year"].astype(str) + "-" + cal["Quarter"]
    cal["MonthNum"] = cal["Date"].dt.month
    cal["MonthName"] = cal["Date"].dt.strftime("%b")
    cal["YearMonth"] = cal["Date"].dt.strftime("%Y-%m")
    cal["MonthStart"] = cal["Date"].dt.to_period("M").dt.to_timestamp()
    cal["WeekdayNum"] = cal["Date"].dt.dayofweek + 1  # 1 = Senin
    cal["WeekdayName"] = cal["Date"].dt.strftime("%a")
    cal["IsWeekend"] = cal["WeekdayNum"] >= 6
    month_end = cal["MonthStart"] + pd.offsets.MonthEnd(0)
    # Bulan dianggap lengkap bila data mencakup seluruh harinya (bulan awal harus mulai tgl 1,
    # bulan akhir harus sampai akhir bulan).
    first_month = d_min.replace(day=1)
    cal["IsCompleteMonth"] = ~((month_end > d_max) | ((cal["MonthStart"] == first_month) & (d_min.day > 1)))

    prod_codes = pd.concat([sales["StockCode"], returns["StockCode"]]).unique()
    first_sold = sales.groupby("StockCode")["InvoiceDateOnly"].min()
    med_price = sales.groupby("StockCode")["UnitPrice"].median()
    dim_product = pd.DataFrame({"StockCode": sorted(prod_codes)})
    dim_product["Description"] = dim_product["StockCode"].map(best_desc).fillna("UNKNOWN PRODUCT")
    dim_product["FirstSoldDate"] = dim_product["StockCode"].map(first_sold)
    dim_product["MedianUnitPrice"] = dim_product["StockCode"].map(med_price).round(4)

    def mode_by(df_: pd.DataFrame, key: str, val: str) -> pd.Series:
        c = (df_.groupby([key, val]).size().reset_index(name="n")
             .sort_values([key, "n", val], ascending=[True, False, True]).drop_duplicates(key))
        return c.set_index(key)[val]

    cust_rows = pd.concat([sales[sales["HasCustomer"]], returns[returns["HasCustomer"]]])
    cust_country = mode_by(cust_rows, "CustomerID", "Country")
    multi_country = int((cust_rows.groupby("CustomerID")["Country"].nunique() > 1).sum())
    sc = sales[sales["HasCustomer"]]
    agg = sc.groupby("CustomerID").agg(FirstPurchaseDate=("InvoiceDateOnly", "min"),
                                       LastPurchaseDate=("InvoiceDateOnly", "max"),
                                       TotalOrders=("InvoiceNo", "nunique"),
                                       TotalRevenue=("LineRevenue", "sum"))
    dim_customer = pd.DataFrame({"CustomerID": cust_country.index}).set_index("CustomerID")
    dim_customer["Country"] = cust_country
    dim_customer = dim_customer.join(agg)
    dim_customer["CohortMonth"] = dim_customer["FirstPurchaseDate"].dt.to_period("M").dt.to_timestamp()
    dim_customer["TotalRevenue"] = dim_customer["TotalRevenue"].round(2)
    dim_customer["TotalOrders"] = dim_customer["TotalOrders"].astype("Int64")
    dim_customer = dim_customer.reset_index().sort_values("CustomerID")

    countries = pd.concat([sales["Country"], returns["Country"]]).dropna().unique()
    dim_country = pd.DataFrame({"Country": sorted(countries)})
    dim_country["IsUnitedKingdom"] = dim_country["Country"].eq("United Kingdom")
    dim_country["MarketGroup"] = np.where(dim_country["IsUnitedKingdom"], "Domestic (UK)", "International")

    log.add("Bangun tabel dimensi", "dim_date, dim_product, dim_customer, dim_country", "Tabel baru",
            len(sales), 0, len(sales), 0,
            f"{multi_country:,} pelanggan muncul di >1 negara; dim_customer memakai negara terbanyak. "
            f"Bulan tidak lengkap ditandai di dim_date.IsCompleteMonth.")

    # ---- Ekspor ----
    print("\n== Menulis file ==")

    def cust_text(s: pd.Series) -> pd.Series:
        return s.astype("Int64").astype("string")

    fs = sales.sort_values(["InvoiceDate", "InvoiceNo"]).reset_index(drop=True)
    fs.insert(0, "SalesLineID", np.arange(1, len(fs) + 1))
    fs["CustomerID"] = cust_text(fs["CustomerID"])
    fs = fs.rename(columns={"InvoiceDate": "InvoiceDateTime", "InvoiceDateOnly": "InvoiceDate"})
    fs = fs[["SalesLineID", "InvoiceNo", "InvoiceDateTime", "InvoiceDate", "StockCode", "Description",
             "Quantity", "UnitPrice", "LineRevenue", "CustomerID", "HasCustomer", "Country", "IsOutlier",
             "RawRowNo"]]

    fr = returns.sort_values(["InvoiceDate", "InvoiceNo"]).reset_index(drop=True)
    fr.insert(0, "ReturnLineID", np.arange(1, len(fr) + 1))
    fr["CustomerID"] = cust_text(fr["CustomerID"])
    fr = fr.rename(columns={"InvoiceDate": "InvoiceDateTime", "InvoiceDateOnly": "InvoiceDate"})
    fr = fr[["ReturnLineID", "InvoiceNo", "InvoiceDateTime", "InvoiceDate", "StockCode", "Description",
             "ReturnQty", "UnitPrice", "ReturnValue", "CustomerID", "HasCustomer", "Country", "RawRowNo"]]

    npi = non_product.sort_values(["InvoiceDate", "InvoiceNo"]).copy()
    npi["CustomerID"] = cust_text(npi["CustomerID"])
    npi["LineValue"] = (npi["Quantity"] * npi["UnitPrice"]).round(4)
    npi["IsCancellation"] = npi["InvoiceNo"].str.upper().str.startswith("C").fillna(False)
    npi = npi.rename(columns={"InvoiceDate": "InvoiceDateTime"})[
        ["InvoiceNo", "InvoiceDateTime", "StockCode", "Description", "NonProductCategory", "Quantity",
         "UnitPrice", "LineValue", "IsCancellation", "CustomerID", "Country", "RawRowNo"]]

    rej = pd.concat(rejects, ignore_index=True) if rejects else pd.DataFrame()
    if len(rej):
        rej["CustomerID"] = cust_text(rej["CustomerID"])
        rej = rej[["RawRowNo", "RejectReason", "InvoiceNo", "InvoiceDate", "StockCode", "Description",
                   "Quantity", "UnitPrice", "CustomerID", "Country"]].sort_values("RawRowNo")

    dim_customer["CustomerID"] = cust_text(dim_customer["CustomerID"])

    def save(df_: pd.DataFrame, name: str) -> None:
        df_ = df_.copy()
        # Kolom tanggal: tanpa jam bila semuanya tengah malam (aman untuk relasi ke dim_date)
        for c in df_.select_dtypes(include=["datetime", "datetimetz"]).columns:
            has_time = (df_[c].dropna() != df_[c].dropna().dt.normalize()).any()
            df_[c] = df_[c].dt.strftime("%Y-%m-%d %H:%M:%S" if has_time else "%Y-%m-%d")
        df_.to_csv(out_dir / name, index=False, encoding="utf-8-sig")
        print(f"  {name:<26} {len(df_):>10,} baris")

    for df_, name in [(fs, "fact_sales.csv"), (fr, "fact_returns.csv"), (npi, "non_product_items.csv"),
                      (rej, "rejected_rows.csv"), (cal, "dim_date.csv"), (dim_product, "dim_product.csv"),
                      (dim_customer, "dim_customer.csv"), (dim_country, "dim_country.csv")]:
        save(df_, name)

    qlog = log.to_df()
    qlog.to_csv(out_dir / "data_quality_log.csv", index=False, encoding="utf-8-sig")
    profile.to_csv(out_dir / "data_quality_profile.csv", index=False, encoding="utf-8-sig")

    # ---- Laporan markdown ----
    tot_sales = float(fs["LineRevenue"].sum())
    tot_ret = float(fr["ReturnValue"].sum())
    reject_summary = (rej.groupby("RejectReason").size().reset_index(name="rows") if len(rej)
                      else pd.DataFrame({"RejectReason": [], "rows": []}))
    last_complete = cal.loc[cal["IsCompleteMonth"], "YearMonth"].max()
    report = f"""# Data Quality Report: Online Retail II

Dibuat: {datetime.now():%Y-%m-%d %H:%M} | Sumber: `{input_path.name}`

## Ringkasan hasil
| Tabel | Baris |
|---|---|
| Data mentah | {raw_rows:,} |
| fact_sales | {len(fs):,} |
| fact_returns | {len(fr):,} |
| non_product_items | {len(npi):,} |
| rejected_rows | {len(rej):,} |

- Gross revenue (fact_sales): **{tot_sales:,.2f}**
- Nilai retur (fact_returns): **{tot_ret:,.2f}** ({tot_ret / tot_sales:.2%} dari gross revenue)
- Net revenue: **{tot_sales - tot_ret:,.2f}**
- Pelanggan dikenal: {fs['CustomerID'].nunique():,}; baris tanpa CustomerID: {int((~fs['HasCustomer']).sum()):,} ({(~fs['HasCustomer']).mean():.1%} dari baris penjualan)
- Rentang data: {d_min:%Y-%m-%d} sampai {d_max:%Y-%m-%d}. Bulan lengkap terakhir: **{last_complete}**.

## Profil data mentah
{md_table(profile)}

## Log langkah cleaning
{md_table(qlog[['step_no', 'step', 'action', 'rows_affected', 'rows_after', 'value_impact']])}

Detail aturan dan catatan tiap langkah ada di `data_quality_log.csv`.

## Rincian baris yang dibuang
{md_table(reject_summary)}

Semua baris yang dibuang disimpan di `rejected_rows.csv` lengkap dengan nomor baris di file sumber (`RawRowNo`).

## Rekonsiliasi
Jumlah baris di fact_sales + fact_returns + non_product_items + rejected_rows = {total_out:,} = jumlah baris mentah. Total nilai (Qty x Price) semua tabel sama dengan nilai mentah ({raw_value:,.2f}).

## Catatan penggunaan di Power BI
1. Import semua CSV (encoding UTF-8). Set `CustomerID`, `InvoiceNo`, `StockCode` sebagai **Text**.
2. Relasi (many-to-one, filter satu arah dari dimensi ke fakta):
   - `fact_sales[InvoiceDate]` -> `dim_date[Date]` (tandai dim_date sebagai Date table)
   - `fact_returns[InvoiceDate]` -> `dim_date[Date]`
   - `fact_sales[StockCode]` / `fact_returns[StockCode]` -> `dim_product[StockCode]`
   - `fact_sales[CustomerID]` / `fact_returns[CustomerID]` -> `dim_customer[CustomerID]`
   - `fact_sales[Country]` / `fact_returns[Country]` -> `dim_country[Country]`
3. Untuk RFM, cohort, dan retensi: filter `HasCustomer = TRUE`.
4. Untuk perbandingan YoY/MoM: pakai `dim_date[IsCompleteMonth] = TRUE` agar bulan terakhir yang belum lengkap tidak menyesatkan.
5. `IsOutlier` untuk membandingkan metrik dengan/tanpa order ekstrem.
"""
    (out_dir / "data_quality_report.md").write_text(report, encoding="utf-8")
    print("  data_quality_log.csv / data_quality_profile.csv / data_quality_report.md")
    print(f"\nSelesai. Semua file ada di: {out_dir.resolve()}\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="Cleaning Online Retail II untuk Power BI")
    ap.add_argument("--input", "-i", required=True, type=Path, help="Path online_retail_II.csv")
    ap.add_argument("--output", "-o", default=Path("clean_output"), type=Path, help="Folder output")
    args = ap.parse_args()
    if not args.input.exists():
        print(f"File tidak ditemukan: {args.input}", file=sys.stderr)
        return 1
    run(args.input, args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
