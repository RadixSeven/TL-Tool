Tools for Tech Leads - mainly from Jira

# Development Setup

Create and activate a virtual environment:

```bash
virtualenv -p python3.11 ~/.virtualenvs/TLTool --no-site-packages
source ~/.virtualenvs/TLTool/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

After activating a virtual environment using the requirements in
requirements.txt (updated with `bazel run
//:requirements.update` from `requirements.in`), install the `pre-commit` hook.

```bash
pre-commit install
```

# Note on Python Version

Only supports Python 3.11 because that's all that is supported in
rules_python for `bzlmod`.
