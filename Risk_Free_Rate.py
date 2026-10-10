#====================================================================================
# Project: FDL-001                                                                  ||
# Title: Estimation of the Risk-Free Rate from Iranian Fixed-Income Market Data     ||
# Author: Khashayar Jamshidi                                                        ||
# Role: Financial Data Analyst                                                      ||
#====================================================================================
import requests                 # for receiving a web page
import pandas as pd             # for data manipulation
import numpy as np
import jdatetime
from io import StringIO         # for reading the data into a pandas dataframe
from datetime import datetime
import matplotlib.pyplot as plt
from openpyxl.drawing.image import Image

# ------------------------------- Settings ----------------------------------------
URL            = "https://www.ifb.ir/ytm.aspx"
INCLUDE_WORDS  = ["اخزا", "گام"]     # instruments to keep (use ["اخزا"] only for a stricter benchmark)
EXCLUDE_WORD   = "سررسید شده"        # matured instruments
MAX_STALE_DAYS = 5                   # max days since last trade (older prices are dropped)
MIN_DAYS       = 7                   # drop instruments that are too close to maturity
MIN_YTM        = 20                  # outlier filter (e.g. the 5.89% row)
RATE_COL       = "YTM"               # "YTM" or "Simple Yield" (use the same one for all tenors)
TARGETS        = {"Short (90d)": 90, "Medium (1y)": 365, "Medium (2y)": 730}
LAMBDA_GRID    = np.arange(0.05, 10.0, 0.01)   # Nelson-Siegel decay parameter search range (years)
EXTRAPOLATE    = False               # if False, rates outside the data range are NaN
OUTPUT_FILE    = "Output.xlsx"

# ------------------------------- Helpers -----------------------------------------
def to_num(s):
    """'40/05%' -> 40.05"""
    return pd.to_numeric(
        s.astype(str).str.replace("%", "", regex=False)
                     .str.replace("/", ".", regex=False).str.strip(),
        errors="coerce")

def jalali_to_date(s):
    """'1405-07-18' -> gregorian date; invalid day is moved back to the last valid day of the month."""
    try:
        y, m, d = map(int, str(s).strip().split("-"))
    except ValueError:
        return pd.NaT
    while d > 0:
        try:
            return jdatetime.date(y, m, d).togregorian()
        except ValueError:
            d -= 1
    return pd.NaT

def ns_design(t, lam):
    """Nelson-Siegel design matrix. t in years, lam in years."""
    x = t / lam
    f1 = (1 - np.exp(-x)) / x
    f2 = f1 - np.exp(-x)
    return np.column_stack([np.ones_like(t), f1, f2])

def fit_nelson_siegel(t, y, lam_grid):
    """For each lambda solve beta0, beta1, beta2 by least squares; keep the best lambda."""
    best = None
    for lam in lam_grid:
        X = ns_design(t, lam)
        beta, *_ = np.linalg.lstsq(X, y, rcond=None)
        sse = float(((y - X @ beta) ** 2).sum())
        if best is None or sse < best["sse"]:
            best = {"beta0": beta[0], "beta1": beta[1], "beta2": beta[2], "lambda": lam, "sse": sse}
    return best

def ns_rate(days, p):
    """Nelson-Siegel rate (%) for days to maturity."""
    t = np.atleast_1d(np.asarray(days, dtype=float)) / 365.0
    X = ns_design(t, p["lambda"])
    return X @ np.array([p["beta0"], p["beta1"], p["beta2"]])

def save_excel(path, sheets, image_path=None):
    """Save sheets; if the file is open/locked, save with a timestamped name instead."""
    try:
        writer = pd.ExcelWriter(path, engine="openpyxl")
    except PermissionError:
        path = f"Output_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
        print(f"{OUTPUT_FILE} is open or locked. Saving as {path} instead.")
        writer = pd.ExcelWriter(path, engine="openpyxl")
    with writer:
        for name, frame in sheets.items():
            frame.to_excel(writer, sheet_name=name, index=False)
        if image_path:
            ws = writer.book.create_sheet("Chart")
            ws.add_image(Image(image_path), "A1")
    print(f"{path} saved.")

# ------------------------------- Download ----------------------------------------
headers = {"User-Agent": "Mozilla/5.0"}
response = requests.get(URL, headers=headers, timeout=30)   # send a GET request to the URL
response.raise_for_status()
response.encoding = response.apparent_encoding               # fix Persian text encoding

tables = pd.read_html(StringIO(response.text))               # list of dataframes
print(f"Number of tables found: {len(tables)}")
df = tables[1]

# ------------------------------- Filter rows -------------------------------------
col  = df.iloc[:, 1].astype(str)     # name column
col2 = df.iloc[:, 5].astype(str)     # status column
pattern = "|".join(INCLUDE_WORDS)
filtered = df[col.str.contains(pattern) & ~col2.str.contains(EXCLUDE_WORD)].copy()

# ------------------------------- Rename headers ----------------------------------
filtered.columns = [
    str(c).strip()
    .replace("ردیف", "index")
    .replace("نماد", "Name")
    .replace("قیمت معامله شده هر ورقه", "Last Price")
    .replace("تاریخ آخرین روز معاملاتی", "Date")
    .replace("تاریخ سررسید", "Maturity")
    .replace("بازده ساده", "Simple Yield")
    for c in filtered.columns
]
print(list(filtered.columns))        # check the final header names

required = ["Name", "Date", "Maturity", "YTM", "Simple Yield"]
missing = [c for c in required if c not in filtered.columns]
if missing:
    raise KeyError(f"Columns not found: {missing}. Rename them in the header step above.")

# ------------------------------- Clean data --------------------------------------
for c in ["YTM", "Simple Yield", "Last Price"]:
    if c in filtered.columns:
        filtered[c] = to_num(filtered[c])

filtered["trade_date"]    = pd.to_datetime(filtered["Date"].apply(jalali_to_date))
filtered["maturity_date"] = pd.to_datetime(filtered["Maturity"].apply(jalali_to_date))
filtered = filtered.dropna(subset=["trade_date", "maturity_date", RATE_COL])

base = filtered["trade_date"].max()                          # latest trading day in the data
filtered["days_to_maturity"] = (filtered["maturity_date"] - base).dt.days
filtered["days_since_trade"] = (base - filtered["trade_date"]).dt.days

clean = filtered[
    (filtered["days_since_trade"] <= MAX_STALE_DAYS) &
    (filtered["days_to_maturity"] >= MIN_DAYS) &
    (filtered[RATE_COL] > MIN_YTM)
].copy()

print(f"Base date: {base.date()} | rows before: {len(filtered)} | rows after cleaning: {len(clean)}")
if len(clean) < 4:
    raise ValueError("Not enough clean data points to fit Nelson-Siegel (need at least 4).")

# ------------------------------- Linear curve (for comparison) -------------------
curve = (clean.groupby("days_to_maturity", as_index=False)[RATE_COL].mean()
              .sort_values("days_to_maturity"))

d_min = int(clean["days_to_maturity"].min())
d_max = int(clean["days_to_maturity"].max())

def linear_rate_at(target_days):
    if target_days < d_min or target_days > d_max:
        return np.nan
    return float(np.interp(target_days, curve["days_to_maturity"], curve[RATE_COL]))

# ------------------------------- Nelson-Siegel fit -------------------------------
t_obs = clean["days_to_maturity"].to_numpy(dtype=float) / 365.0
y_obs = clean[RATE_COL].to_numpy(dtype=float)

params = fit_nelson_siegel(t_obs, y_obs, LAMBDA_GRID)
y_fit = ns_rate(clean["days_to_maturity"].to_numpy(), params)
sst   = float(((y_obs - y_obs.mean()) ** 2).sum())
rmse  = float(np.sqrt(params["sse"] / len(y_obs)))
r2    = 1 - params["sse"] / sst if sst > 0 else np.nan

clean["NS Fitted"]   = np.round(y_fit, 4)
clean["NS Residual"] = np.round(clean[RATE_COL] - clean["NS Fitted"], 4)

ns_params = pd.DataFrame({
    "Parameter": ["beta0 (long-term level)", "beta1 (slope)", "beta2 (curvature)",
                  "lambda (years)", "RMSE (% points)", "R-squared", "N points"],
    "Value": [params["beta0"], params["beta1"], params["beta2"],
              params["lambda"], rmse, r2, len(y_obs)],
})
print(ns_params.to_string(index=False))

# short-end / long-end sanity check
print(f"Implied short-end rate (tau->0): {params['beta0'] + params['beta1']:.2f}% | "
      f"long-end rate (tau->inf): {params['beta0']:.2f}%")

# ------------------------------- Risk-free rates ---------------------------------
targets = dict(TARGETS)
targets["Long (longest available)"] = d_max

def ns_rate_at(target_days):
    if not EXTRAPOLATE and (target_days < d_min or target_days > d_max):
        return np.nan
    return float(ns_rate(target_days, params)[0])

rates = pd.DataFrame({
    "Tenor": list(targets.keys()),
    "Days": list(targets.values()),
    f"NS Risk-Free Rate ({RATE_COL}) %": [round(ns_rate_at(d), 2) for d in targets.values()],
    f"Linear Interp ({RATE_COL}) %":     [round(linear_rate_at(d), 2) for d in targets.values()],
})
print(rates)

# fitted curve on a daily grid (for Excel and plot)
grid_days = np.arange(max(d_min, 1), d_max + 1)
ns_curve = pd.DataFrame({"days_to_maturity": grid_days,
                         f"NS Fitted {RATE_COL} %": np.round(ns_rate(grid_days, params), 4)})

# ------------------------------- Plot --------------------------------------------
fig, ax = plt.subplots(figsize=(11, 6))

# 1) all cleaned instruments (separated by type)
for word, color, marker, label in [("اخزا", "tab:blue", "o", "Akhza"),
                                   ("گام", "tab:orange", "s", "Gam")]:
    sub = clean[clean["Name"].astype(str).str.contains(word)]
    if not sub.empty:
        ax.scatter(sub["days_to_maturity"], sub[RATE_COL],
                   color=color, marker=marker, alpha=0.6, s=45, label=label)

# 2) linear curve (reference)
ax.plot(curve["days_to_maturity"], curve[RATE_COL],
        color="gray", linewidth=1.0, linestyle=":", label="Linear (avg per maturity)")

# 3) Nelson-Siegel curve
ax.plot(ns_curve["days_to_maturity"], ns_curve[f"NS Fitted {RATE_COL} %"],
        color="black", linewidth=2.2, label="Nelson-Siegel fit")

# 4) risk-free rate at target tenors
ns_col = f"NS Risk-Free Rate ({RATE_COL}) %"
valid = rates.dropna(subset=[ns_col])
ax.scatter(valid["Days"], valid[ns_col],
           color="red", zorder=5, s=90, edgecolors="black", label="Target tenors (NS)")
for _, r in valid.iterrows():
    ax.annotate(f"{r[ns_col]:.2f}%", (r["Days"], r[ns_col]),
                textcoords="offset points", xytext=(0, 10),
                ha="center", fontsize=9, color="red")

ax.set_title(f"Iran Government Securities Yield Curve - Nelson-Siegel ({RATE_COL}) - {base.date()}\n"
             f"lambda={params['lambda']:.2f}y | RMSE={rmse:.2f} | R²={r2:.3f}")
ax.set_xlabel("Days to Maturity")
ax.set_ylabel(f"{RATE_COL} (%)")
ax.grid(True, linestyle="--", alpha=0.5)
ax.legend()
plt.tight_layout()
plt.savefig("Yield_Curve.png", dpi=150)

# ------------------------------- Save --------------------------------------------
save_excel(
    OUTPUT_FILE,
    {
        "Clean Data": clean,
        "Linear Curve": curve,
        "NS Parameters": ns_params,
        "NS Curve": ns_curve,
        "Risk-Free Rates": rates,
    },
    image_path="Yield_Curve.png",
)

plt.show()