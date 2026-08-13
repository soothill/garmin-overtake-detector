# Contributing

Contributions are welcome. Please keep fixtures synthetic or fully anonymized;
never submit real number plates, faces, GPS overlays or private manifests.

Before opening a pull request:

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile *.py
bash -n ./*.sh scripts/*.sh lib/*.sh
```

Changes to matching, validation or retry behavior should include a regression
test and explain how ambiguous handoffs remain fail-closed.
