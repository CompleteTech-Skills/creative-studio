# Synthetic Director contract fixtures

`contracts.json` contains all six core document families and 32 strict method
request/result pairs. The six accompanying document files and `evidence.txt`
are exact-byte references. They are self-authored engineering fixtures with no
third-party assets. No referenced "render", "review", provider, job or payment
was executed; success-shaped values test contract admission only.

Run from the repository root:

```bash
python scripts/director_contracts.py --bundle examples/director-contracts/contracts.json --root .
```

The example uses 30 seconds at 24 FPS, frames 0–719 and terminal boundary 720.
This time contract is real arithmetic; it is not evidence of a rendered film.
See [the contract guide](../../references/director-contracts.md) for interfaces,
limits and the distinction between structural, exact-byte and native acceptance.
