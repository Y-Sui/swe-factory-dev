# SWE-bench Benchmark Inspector

Streamlit dashboard for inspecting SWE-bench instance quality — problem statements, gold patches, test patches, and Docker-based coverage.

## Setup

```bash
pip install -r bench-inspector/requirements.txt
```

## Run

```bash
python3 -m streamlit run app.py --server.headless true --server.port 8888 --server.enableCORS false --server.enableXsrfProtection false
```

Opens at `http://localhost:8888`. If using VS Code Remote SSH, forward port `8888` in the Ports panel.
