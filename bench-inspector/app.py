"""SWE-bench Benchmark Quality Inspector — Streamlit App."""

import json
import streamlit as st
import pandas as pd
import plotly.express as px

from analyzer import (
    load_instances,
    check_problem_statement,
    check_patch,
    check_test_patch,
    compute_dataset_summary,
)
from coverage_runner import run_coverage_in_docker, run_all_coverage, format_coverage_summary, build_all_images, image_exists

st.set_page_config(page_title="SWE-bench Inspector", layout="wide")

# ---------------------------------------------------------------------------
# Sidebar — file loader
# ---------------------------------------------------------------------------
st.sidebar.title("SWE-bench Inspector")

DEFAULT_FILES = {
    "swe-smith (JSONL)": "/home/yuansui/swe-factory-dev/swe-smith-dev/output/swe_smith_instances.jsonl",
    "internal-bench v1 (JSON)": "/home/yuansui/swe-factory-dev/internal-swe-bench-data/results_v1_gpt_5_2_68_20260307_verified.json",
}

source = st.sidebar.radio("Data source", ["Preset files", "Custom path", "Upload"])

if source == "Preset files":
    chosen = st.sidebar.selectbox("Select file", list(DEFAULT_FILES.keys()))
    file_path = DEFAULT_FILES[chosen]
elif source == "Custom path":
    file_path = st.sidebar.text_input("File path", value="")
else:
    uploaded = st.sidebar.file_uploader("Upload JSON / JSONL", type=["json", "jsonl"])
    file_path = None

# Load data
instances = []
load_error = None
try:
    if source == "Upload" and uploaded is not None:
        text = uploaded.read().decode("utf-8")
        if uploaded.name.endswith(".jsonl"):
            instances = [json.loads(l) for l in text.strip().splitlines() if l.strip()]
        else:
            data = json.loads(text)
            instances = data if isinstance(data, list) else [data]
    elif file_path:
        instances = load_instances(file_path)
except Exception as e:
    load_error = str(e)

if load_error:
    st.error(f"Failed to load data: {load_error}")
    st.stop()
if not instances:
    st.info("Select or upload a dataset to begin.")
    st.stop()

# ---------------------------------------------------------------------------
# Precompute reports
# ---------------------------------------------------------------------------
ps_reports = [check_problem_statement(i) for i in instances]
patch_reports = [check_patch(i) for i in instances]
test_reports = [check_test_patch(i) for i in instances]
summary = compute_dataset_summary(instances)

# ---------------------------------------------------------------------------
# Sidebar — batch build Docker images
# ---------------------------------------------------------------------------
st.sidebar.markdown("---")
st.sidebar.subheader("Docker Images")
cached_count = sum(1 for i in instances if image_exists(i))
st.sidebar.write(f"Cached: {cached_count} / {len(instances)}")

if st.sidebar.button("Build All Images"):
    progress = st.sidebar.progress(0, text="Building...")
    status_area = st.sidebar.empty()

    def _cb(done, total, iid, status):
        progress.progress(done / total, text=f"{done}/{total}: {iid[:30]}")
        status_area.text(f"{iid[:30]}: {status}")

    results = build_all_images(instances, progress_callback=_cb)
    ok_count = sum(1 for v in results.values() if v in ("ok", "cached"))
    st.sidebar.success(f"Done: {ok_count}/{len(results)} images ready")
    failed = {k: v for k, v in results.items() if v not in ("ok", "cached")}
    if failed:
        st.sidebar.error(f"{len(failed)} failed")
        for iid, msg in failed.items():
            st.sidebar.text(f"  {iid[:30]}: {msg[:80]}")

# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------
tab_overview, tab_ps, tab_patch, tab_test, tab_detail = st.tabs([
    "Overview", "Problem Statements", "Gold Patches", "Test Patches", "Instance Detail"
])

# ========================== OVERVIEW ========================================
with tab_overview:
    st.header("Dataset Overview")
    cols = st.columns(4)
    cols[0].metric("Total Instances", summary["total_instances"])
    cols[1].metric("Avg PS Length", f"{summary['problem_statement']['avg_length']:.0f} chars")
    cols[2].metric("Avg Patch Lines", f"{summary['patch']['avg_lines']:.1f}")
    cols[3].metric("Avg Test Count", f"{summary['test_patch']['avg_test_count']:.1f}")

    st.subheader("Repo Distribution")
    repo_df = pd.DataFrame(list(summary["repos"].items()), columns=["Repo", "Count"])
    st.plotly_chart(px.pie(repo_df, names="Repo", values="Count", hole=0.4), use_container_width=True)

    st.subheader("Key Stats")
    issue_data = [
        {"Category": "Patch", "Metric": "Empty patches", "Count": summary["patch"]["empty_count"]},
        {"Category": "Test", "Metric": "No FAIL_TO_PASS", "Count": summary["test_patch"]["no_f2p_count"]},
        {"Category": "Test", "Metric": "No PASS_TO_PASS", "Count": summary["test_patch"]["no_p2p_count"]},
    ]
    st.dataframe(pd.DataFrame(issue_data), use_container_width=True, hide_index=True)

    st.subheader("Per-Instance Summary")
    overview_df = pd.DataFrame({
        "instance_id": [i.get("instance_id", "") for i in instances],
        "Repo": [i.get("repo", "") for i in instances],
        "PS Words": [r.word_count for r in ps_reports],
        "Patch Lines": [r.stats["total_lines"] for r in patch_reports],
        "Test Functions": [len(r.test_names) for r in test_reports],
        "F2P": [r.f2p_count for r in test_reports],
        "P2P": [r.p2p_count for r in test_reports],
    })
    st.dataframe(overview_df, use_container_width=True, hide_index=True, height=400)


# ========================== PROBLEM STATEMENTS ==============================
with tab_ps:
    st.header("Problem Statement Quality")

    # Length distribution
    ps_lengths = [r.word_count for r in ps_reports]
    st.plotly_chart(
        px.histogram(x=ps_lengths, nbins=20, labels={"x": "Word Count"}, title="Word Count Distribution"),
        use_container_width=True,
    )

    ps_table = []
    for r in ps_reports:
        ps_table.append({
            "instance_id": r.instance_id,
            "Length": r.length,
            "Words": r.word_count,
            "Steps to Repro": "✓" if r.has_steps_to_reproduce else "✗",
            "Expected Behavior": "✓" if r.has_expected_behavior else "✗",
            "Actual Behavior": "✓" if r.has_actual_behavior else "✗",
            "Error Msg": "✓" if r.has_error_message else "✗",
            "Code Snippet": "✓" if r.has_code_snippet else "✗",
        })
    st.dataframe(pd.DataFrame(ps_table), use_container_width=True, hide_index=True, height=500)


# ========================== GOLD PATCHES ====================================
with tab_patch:
    st.header("Gold Patch Quality")

    c1, c2, c3 = st.columns(3)
    c1.metric("Avg Diff Lines", f"{summary['patch']['avg_lines']:.1f}")
    c2.metric("Avg Files Changed", f"{summary['patch']['avg_files']:.1f}")
    c3.metric("Empty Patches", summary["patch"]["empty_count"])

    patch_lines = [r.stats["total_lines"] for r in patch_reports]
    st.plotly_chart(
        px.histogram(x=patch_lines, nbins=30, labels={"x": "Diff Lines"}, title="Patch Size Distribution"),
        use_container_width=True,
    )

    patch_table = []
    for r in patch_reports:
        patch_table.append({
            "instance_id": r.instance_id,
            "Files": r.stats["files"],
            "Additions": r.stats["additions"],
            "Deletions": r.stats["deletions"],
            "Total Lines": r.stats["total_lines"],
        })
    st.dataframe(pd.DataFrame(patch_table), use_container_width=True, hide_index=True, height=500)

    # Batch coverage
    st.markdown("---")
    st.subheader("Batch Coverage")
    cov_workers = st.number_input("Parallel workers", min_value=1, max_value=32, value=16, key="cov_workers")

    if st.button("Run All Coverage"):
        coverable = [i for i in instances if i.get("dockerfile") or i.get("docker_template")]
        if not coverable:
            st.warning("No instances have Dockerfiles")
        else:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            progress = st.progress(0, text="Running coverage...")
            status_area = st.empty()
            total = len(coverable)
            cov_results = {}

            with ThreadPoolExecutor(max_workers=cov_workers) as pool:
                futures = {
                    pool.submit(run_coverage_in_docker, inst): inst.get("instance_id", "unknown")
                    for inst in coverable
                }
                for i, future in enumerate(as_completed(futures), 1):
                    iid = futures[future]
                    result = future.result()
                    cov_results[iid] = result
                    progress.progress(i / total, text=f"{i}/{total}: {iid[:30]}")
                    cov = result.get("coverage", {})
                    pct = cov.get("totals", {}).get("percent_covered")
                    label = f"{pct:.1f}%" if pct is not None else ("error" if result.get("error") else "done")
                    status_area.text(f"{iid[:30]}: {label}")

            # Show summary table
            cov_rows = []
            for iid, res in cov_results.items():
                patch = next((i.get("patch", "") for i in instances if i.get("instance_id") == iid), "")
                summary_cov = format_coverage_summary(res.get("coverage", {}), patch=patch)
                cov_rows.append({
                    "instance_id": iid,
                    "coverage%": f"{summary_cov['total_coverage']:.1f}" if summary_cov["total_coverage"] is not None else "N/A",
                    "stmts": summary_cov.get("total_stmts", 0),
                    "miss": summary_cov.get("total_miss", 0),
                    "exit_code": res.get("exit_code", ""),
                })
            st.session_state["batch_cov_results"] = cov_rows
            st.success(f"Done: {len(cov_results)} instances")

    if "batch_cov_results" in st.session_state:
        st.dataframe(pd.DataFrame(st.session_state["batch_cov_results"]), use_container_width=True, hide_index=True, height=300)


# ========================== TEST PATCHES ====================================
with tab_test:
    st.header("Test Patch Quality")

    c1, c2, c3 = st.columns(3)
    c1.metric("Avg Test Count", f"{summary['test_patch']['avg_test_count']:.1f}")
    c2.metric("No F2P", summary["test_patch"]["no_f2p_count"])
    c3.metric("No P2P", summary["test_patch"]["no_p2p_count"])

    st.plotly_chart(
        px.scatter(
            x=[r.f2p_count for r in test_reports],
            y=[r.p2p_count for r in test_reports],
            text=[r.instance_id[:30] for r in test_reports],
            labels={"x": "FAIL_TO_PASS count", "y": "PASS_TO_PASS count"},
            title="F2P vs P2P Coverage",
        ),
        use_container_width=True,
    )

    # LLM test quality verdicts (if available)
    quality_verdicts = [r for r in test_reports if r.test_quality_info]
    if quality_verdicts:
        st.subheader("LLM Test Quality Verdicts")
        verdict_table = []
        for r in quality_verdicts:
            tq = r.test_quality_info
            verdict_table.append({
                "instance_id": r.instance_id,
                "Overall": tq.get("overall", "—"),
                "Breadth": tq.get("breadth", {}).get("verdict", "—"),
                "Narrowness": tq.get("narrowness", {}).get("verdict", "—"),
                "Weakness": tq.get("weakness", {}).get("verdict", "—"),
                "Leakage": tq.get("leakage", {}).get("verdict", "—"),
                "Relevance": tq.get("relevance", {}).get("verdict", "—"),
            })
        st.dataframe(pd.DataFrame(verdict_table), use_container_width=True, hide_index=True)

    test_table = []
    for r in test_reports:
        test_table.append({
            "instance_id": r.instance_id,
            "Test Files": len(r.test_files),
            "Test Functions": len(r.test_names),
            "F2P": r.f2p_count,
            "P2P": r.p2p_count,
            "Assertions": "✓" if r.has_assertions else "✗",
        })
    st.dataframe(pd.DataFrame(test_table), use_container_width=True, hide_index=True, height=500)


# ========================== INSTANCE DETAIL =================================
with tab_detail:
    st.header("Instance Detail View")

    instance_ids = [i.get("instance_id", f"idx-{idx}") for idx, i in enumerate(instances)]
    selected_id = st.selectbox("Select instance", instance_ids)
    idx = instance_ids.index(selected_id)
    inst = instances[idx]
    ps_r = ps_reports[idx]
    pa_r = patch_reports[idx]
    te_r = test_reports[idx]

    # Metadata
    with st.expander("Metadata", expanded=False):
        meta_fields = ["instance_id", "repo", "base_commit", "version", "created_at",
                       "commit_sha", "pull_number", "pull_url", "environment_setup_commit"]
        for f in meta_fields:
            if f in inst:
                st.text(f"{f}: {inst[f]}")

    # Problem Statement
    with st.expander("Problem Statement", expanded=True):
        cols = st.columns(5)
        cols[0].write(f"**Length**: {ps_r.length}")
        cols[1].write(f"**Words**: {ps_r.word_count}")
        cols[2].write(f"**Expected**: {'✓' if ps_r.has_expected_behavior else '✗'}")
        cols[3].write(f"**Actual**: {'✓' if ps_r.has_actual_behavior else '✗'}")
        cols[4].write(f"**Repro**: {'✓' if ps_r.has_steps_to_reproduce else '✗'}")
        st.markdown("---")
        st.markdown(inst.get("problem_statement", "*empty*"))

    # Gold Patch
    with st.expander("Gold Patch", expanded=False):
        st.write(f"**Files**: {pa_r.stats['files']} | **+{pa_r.stats['additions']}/-{pa_r.stats['deletions']}** | **Total**: {pa_r.stats['total_lines']} lines")
        if pa_r.stats["files_list"]:
            st.write("Files: " + ", ".join(f"`{f}`" for f in pa_r.stats["files_list"]))
        st.code(inst.get("patch", ""), language="diff")

    # Mask patch (swe-smith)
    if inst.get("mask_patch"):
        with st.expander("Mask Patch", expanded=False):
            st.code(inst["mask_patch"], language="diff")

    # Test Patch
    with st.expander("Test Patch", expanded=False):
        st.write(f"**Test functions**: {len(te_r.test_names)} | **F2P**: {te_r.f2p_count} | **P2P**: {te_r.p2p_count}")
        if te_r.test_names:
            st.write("Tests: " + ", ".join(f"`{t}`" for t in te_r.test_names[:20]))
        st.code(inst.get("test_patch", ""), language="diff")

    # F2P / P2P lists
    with st.expander("F2P / P2P Test Lists", expanded=False):
        f2p_raw = inst.get("FAIL_TO_PASS", "[]")
        p2p_raw = inst.get("PASS_TO_PASS", "[]")
        f2p_list = json.loads(f2p_raw) if isinstance(f2p_raw, str) else (f2p_raw or [])
        p2p_list = json.loads(p2p_raw) if isinstance(p2p_raw, str) else (p2p_raw or [])
        st.write("**FAIL_TO_PASS:**")
        for t in f2p_list:
            st.code(t, language="text")
        st.write("**PASS_TO_PASS:**")
        for t in p2p_list:
            st.code(t, language="text")

    # Test quality (LLM verdict)
    tq = inst.get("test_quality")
    if tq:
        with st.expander("Test Quality (LLM Verdict)", expanded=False):
            st.json(tq)

    # Coverage runner
    st.subheader("Coverage")
    if inst.get("dockerfile") or inst.get("docker_template"):
        if st.button("Run coverage in Docker", key="run_cov"):
            with st.spinner("Building image & running coverage..."):
                result = run_coverage_in_docker(inst)
            if result.get("error"):
                st.error(result["error"])
            else:
                cov_summary = format_coverage_summary(result.get("coverage", {}), patch=inst.get("patch", ""))
                if cov_summary["total_coverage"] is not None:
                    st.metric("Total Coverage", f"{cov_summary['total_coverage']:.1f}%")
                    if cov_summary["files"]:
                        cov_df = pd.DataFrame(cov_summary["files"])
                        st.dataframe(cov_df, use_container_width=True, hide_index=True)
                else:
                    st.warning("Coverage data not available")
                with st.expander("Raw output", expanded=False):
                    st.code(result.get("output", ""), language="text")
    else:
        st.info("No Dockerfile available — cannot run coverage")

    # Hints
    hints = inst.get("hints_text", "")
    if hints:
        with st.expander("Hints Text", expanded=False):
            st.markdown(hints)

    # Function metadata (swe-smith)
    fm = inst.get("function_metadata")
    if fm:
        with st.expander("Function Metadata", expanded=False):
            st.json(fm)

    # Dockerfile / Docker template
    docker_content = inst.get("dockerfile") or inst.get("docker_template")
    if docker_content:
        with st.expander("Dockerfile", expanded=False):
            st.code(docker_content, language="dockerfile")

    # Eval script
    if inst.get("eval_script"):
        with st.expander("Eval Script", expanded=False):
            st.code(inst["eval_script"], language="bash")
