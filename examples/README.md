# RowGate examples

**All data in this folder is fictional.** The orders and shipments were
made up for these examples; they describe no real people, customers,
orders or shipments.

| File | What it is |
| ---- | ---------- |
| `orders.csv` | 8 invented orders, as CSV |
| `orders.yaml` | rules for `orders.csv`; every check passes (exit code 0) |
| `shipments.parquet` | 8 invented shipments, as Parquet, with problems on purpose |
| `shipments.yaml` | rules for `shipments.parquet`; some checks fail (exit code 1) |
| `make_shipments_parquet.py` | writes `shipments.parquet` from the rows listed in it |

Together the two rules files use all seven rules. Run them from this folder:

```bash
rowgate check orders.yaml
rowgate check shipments.yaml
```

`shipments.parquet` also says it is invented data in its own metadata (the
key `notice`). To write it again: `python make_shipments_parquet.py`.
