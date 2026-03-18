"""SWE-bench Benchmark Quality Inspector — Streamlit App."""

import json
import os
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
from coverage_runner import build_all_images, image_exists, validate_test_in_docker
from annotations import (
    dataset_key_from_path,
    load_reviews,
    save_review,
    load_edits,
    save_edit,
    apply_edits,
    export_merged,
)
from llm_fix import (
    chat_fix,
    make_diff,
    strip_code_fences,
    extract_test_content,
    test_content_to_patch,
    extract_test_ids,
    DEFAULT_MODEL,
)

# Load .env
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
except ImportError:
    pass

st.set_page_config(page_title="SWE-bench Inspector", layout="wide")

# ---------------------------------------------------------------------------
# Sidebar — mode & file loader
# ---------------------------------------------------------------------------
st.sidebar.title("SWE-bench Inspector")

mode = st.sidebar.radio("Mode", ["View", "Review", "Edit"], horizontal=True)

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
dataset_key = ""
try:
    if source == "Upload" and uploaded is not None:
        text = uploaded.read().decode("utf-8")
        if uploaded.name.endswith(".jsonl"):
            instances = [json.loads(l) for l in text.strip().splitlines() if l.strip()]
        else:
            data = json.loads(text)
            instances = data if isinstance(data, list) else [data]
        dataset_key = uploaded.name.rsplit(".", 1)[0]
    elif file_path:
        instances = load_instances(file_path)
        dataset_key = dataset_key_from_path(file_path)
except Exception as e:
    load_error = str(e)

if load_error:
    st.error(f"Failed to load data: {load_error}")
    st.stop()
if not instances:
    st.info("Select or upload a dataset to begin.")
    st.stop()

# Load annotations & apply edits
reviews = load_reviews(dataset_key) if dataset_key else {}
edits = load_edits(dataset_key) if dataset_key else {}
if edits:
    instances = apply_edits(instances, edits)

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
if st.sidebar.button("Check Image Cache"):
    cached_count = sum(1 for i in instances if image_exists(i))
    st.sidebar.write(f"Cached: {cached_count} / {len(instances)}")
else:
    st.sidebar.write(f"Total: {len(instances)} (click Check to count cached)")

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
# Sidebar — LLM model (Edit mode) & Export
# ---------------------------------------------------------------------------
if mode == "Edit":
    st.sidebar.markdown("---")
    st.sidebar.subheader("LLM Settings")
    llm_model = st.sidebar.text_input("Model", value=DEFAULT_MODEL)
else:
    llm_model = DEFAULT_MODEL

if edits or reviews:
    st.sidebar.markdown("---")
    st.sidebar.subheader("Export")
    edited_count = len(edits)
    reviewed_count = len(reviews)
    st.sidebar.write(f"Edited: {edited_count} | Reviewed: {reviewed_count}")
    import re
    from datetime import datetime
    from pathlib import Path as _Path
    base = str(file_path or "export.jsonl")
    # Strip extension, then remove any existing _annotated suffix
    stem = base.rsplit(".", 1)[0]
    stem = re.sub(r"_annotated(_\d{8}(_v\d+)?)?$", "", stem)
    date_str = datetime.now().strftime("%Y%m%d")
    # Find next version number for today
    parent = str(_Path(base).parent)
    version = 1
    while _Path(f"{stem}_annotated_{date_str}_v{version}.jsonl").exists():
        version += 1
    default_export = f"{stem}_annotated_{date_str}_v{version}.jsonl"
    export_path = st.sidebar.text_input("Export path", value=default_export)
    if st.sidebar.button("Export JSONL"):
        try:
            export_merged(instances, reviews, export_path)
            st.sidebar.success(f"Exported {len(instances)} instances")
        except Exception as e:
            st.sidebar.error(str(e))

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
    overview_data = {
        "instance_id": [i.get("instance_id", "") for i in instances],
        "Repo": [i.get("repo", "") for i in instances],
        "PS Words": [r.word_count for r in ps_reports],
        "Patch Lines": [r.stats["total_lines"] for r in patch_reports],
        "Test Functions": [len(r.test_names) for r in test_reports],
        "F2P": [r.f2p_count for r in test_reports],
        "P2P": [r.p2p_count for r in test_reports],
    }
    if reviews:
        overview_data["Review"] = [
            reviews.get(i.get("instance_id", ""), {}).get("score", "")
            for i in instances
        ]
    if edits:
        overview_data["Edited"] = [
            "Yes" if i.get("instance_id", "") in edits else ""
            for i in instances
        ]
    overview_df = pd.DataFrame(overview_data)
    st.dataframe(overview_df, use_container_width=True, hide_index=True, height=400)


# ========================== PROBLEM STATEMENTS ==============================
with tab_ps:
    st.header("Problem Statement Quality")

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


# ── LLM Fix helper (rendered inline in expanders) ─────────────────────────

def _render_llm_fix(instance_id: str, instance: dict, fix_type: str):
    """Render LLM fix chat UI for a given fix type, inside the current expander."""
    st.markdown("---")
    st.caption("LLM Fix")

    chat_key = f"llm_chat_{instance_id}_{fix_type}"
    proposal_key = f"llm_proposal_{instance_id}_{fix_type}"
    if chat_key not in st.session_state:
        st.session_state[chat_key] = []
    if proposal_key not in st.session_state:
        st.session_state[proposal_key] = None

    # Display chat history
    for msg in st.session_state[chat_key]:
        with st.chat_message(msg["role"]):
            st.write(msg["content"])

    # Show diff + action buttons if there's a pending proposal
    if st.session_state[proposal_key] is not None:
        proposed = st.session_state[proposal_key]
        if fix_type == "problem_statement":
            current = instance.get("problem_statement", "")
            diff_text = make_diff(current, proposed, "problem_statement")
        else:
            current_content, _ = extract_test_content(instance.get("test_patch", ""))
            diff_text = make_diff(
                current_content or instance.get("test_patch", ""),
                proposed, "test_file.py",
            )

        st.code(diff_text or "(no changes)", language="diff")

        ac1, ac2, ac3 = st.columns(3)
        if ac1.button("Apply", key=f"apply_{instance_id}_{fix_type}"):
            if fix_type == "problem_statement":
                save_edit(dataset_key, instance_id, {"problem_statement": proposed})
                st.session_state[proposal_key] = None
                st.success("Applied")
                st.rerun()
            else:
                with st.spinner("Validating test in Docker..."):
                    passed, output = validate_test_in_docker(instance, proposed)
                if passed:
                    _, test_fn = extract_test_content(instance.get("test_patch", ""))
                    test_fn = test_fn or "tests/test_generated.py"
                    new_patch = test_content_to_patch(proposed, test_fn)
                    new_ids = extract_test_ids(proposed, test_fn)
                    save_edit(dataset_key, instance_id, {
                        "test_patch": new_patch,
                        "FAIL_TO_PASS": json.dumps(new_ids),
                    })
                    st.session_state[proposal_key] = None
                    st.success("Tests passed — applied")
                    st.rerun()
                else:
                    st.error("Tests failed — sending error back to LLM for retry")
                    feedback = (
                        f"The test file you proposed failed validation in Docker. "
                        f"Fix the issues and return the corrected test file.\n\n"
                        f"## Test output\n```\n{output[-3000:]}\n```"
                    )
                    st.session_state[chat_key].append({"role": "user", "content": feedback})
                    with st.spinner("LLM retrying..."):
                        try:
                            response = chat_fix(
                                st.session_state[chat_key], instance, fix_type, model=llm_model,
                            )
                            response = strip_code_fences(response)
                            st.session_state[chat_key].append({"role": "assistant", "content": response})
                            st.session_state[proposal_key] = response
                        except Exception as e:
                            st.error(f"LLM retry failed: {e}")
                            st.session_state[chat_key].pop()
                    st.rerun()
        if ac2.button("Force Apply", key=f"force_apply_{instance_id}_{fix_type}"):
            if fix_type == "test_files":
                _, test_fn = extract_test_content(instance.get("test_patch", ""))
                test_fn = test_fn or "tests/test_generated.py"
                new_patch = test_content_to_patch(proposed, test_fn)
                new_ids = extract_test_ids(proposed, test_fn)
                save_edit(dataset_key, instance_id, {
                    "test_patch": new_patch,
                    "FAIL_TO_PASS": json.dumps(new_ids),
                })
            else:
                save_edit(dataset_key, instance_id, {"problem_statement": proposed})
            st.session_state[proposal_key] = None
            st.success("Force applied (skipped validation)")
            st.rerun()
        if ac3.button("Reject", key=f"reject_{instance_id}_{fix_type}"):
            st.session_state[proposal_key] = None
            st.rerun()

    # Instruction input + send/clear
    user_instruction = st.text_area(
        "Instruction",
        placeholder="e.g. 'make this more specific' or 'add edge case tests'",
        key=f"fix_instruction_{instance_id}_{fix_type}",
    )
    sc1, sc2 = st.columns([1, 5])
    if sc1.button("Send", key=f"send_fix_{instance_id}_{fix_type}"):
        if not user_instruction.strip():
            st.warning("Enter an instruction")
        else:
            st.session_state[chat_key].append({"role": "user", "content": user_instruction})
            with st.spinner("Calling LLM..."):
                try:
                    response = chat_fix(
                        st.session_state[chat_key], instance, fix_type, model=llm_model,
                    )
                    if fix_type == "test_files":
                        response = strip_code_fences(response)
                    st.session_state[chat_key].append({"role": "assistant", "content": response})
                    st.session_state[proposal_key] = response
                except Exception as e:
                    st.error(f"LLM call failed: {e}")
                    st.session_state[chat_key].pop()
            st.rerun()
    if sc2.button("Clear Chat", key=f"clear_chat_{instance_id}_{fix_type}") and st.session_state[chat_key]:
        st.session_state[chat_key] = []
        st.session_state[proposal_key] = None
        st.rerun()


# ========================== INSTANCE DETAIL =================================
with tab_detail:
    st.header("Instance Detail View")

    reviewed_ids = [i.get("instance_id", "") for i in instances if i.get("instance_id", "") in reviews]
    unreviewed_ids = [i.get("instance_id", f"idx-{idx}") for idx, i in enumerate(instances) if i.get("instance_id", "") not in reviews]
    filter_choice = st.radio("Filter", ["All", f"Unreviewed ({len(unreviewed_ids)})", f"Reviewed ({len(reviewed_ids)})"], horizontal=True)
    if filter_choice.startswith("Unreviewed"):
        filtered_ids = unreviewed_ids
    elif filter_choice.startswith("Reviewed"):
        filtered_ids = reviewed_ids
    else:
        filtered_ids = [i.get("instance_id", f"idx-{idx}") for idx, i in enumerate(instances)]
    if not filtered_ids:
        st.info("No instances in this category.")
        st.stop()
    selected_id = st.selectbox("Select instance", filtered_ids)
    idx = next(idx for idx, i in enumerate(instances) if i.get("instance_id", "") == selected_id)
    inst = instances[idx]
    ps_r = ps_reports[idx]
    pa_r = patch_reports[idx]
    te_r = test_reports[idx]

    # ── Review panel (Review & Edit modes) ────────────────────────────
    if mode in ("Review", "Edit"):
        st.subheader("Review")
        existing_review = reviews.get(selected_id, {})
        score_options = ["—", "good", "bad", "needs-fix"]
        current_score = existing_review.get("score", "—")
        score_idx = score_options.index(current_score) if current_score in score_options else 0

        rc1, rc2 = st.columns([1, 3])
        new_score = rc1.radio(
            "Score", score_options, index=score_idx,
            key=f"review_score_{selected_id}", horizontal=True,
        )
        new_comment = rc2.text_input(
            "Comment", value=existing_review.get("comment", ""),
            key=f"review_comment_{selected_id}",
        )

        if st.button("Save Review", key=f"save_review_{selected_id}"):
            if new_score != "—":
                save_review(dataset_key, selected_id, new_score, new_comment)
                st.success("Review saved")
                st.rerun()
            else:
                st.warning("Select a score first")

        st.markdown("---")

    # ── Metadata ──────────────────────────────────────────────────────
    with st.expander("Metadata", expanded=False):
        meta_fields = ["instance_id", "repo", "base_commit", "version", "created_at",
                       "commit_sha", "pull_number", "pull_url", "environment_setup_commit",
                       "docker_image"]
        for f in meta_fields:
            if f in inst:
                st.text(f"{f}: {inst[f]}")

    # ── Problem Statement ─────────────────────────────────────────────
    with st.expander("Problem Statement", expanded=True):
        cols = st.columns(5)
        cols[0].write(f"**Length**: {ps_r.length}")
        cols[1].write(f"**Words**: {ps_r.word_count}")
        cols[2].write(f"**Expected**: {'✓' if ps_r.has_expected_behavior else '✗'}")
        cols[3].write(f"**Actual**: {'✓' if ps_r.has_actual_behavior else '✗'}")
        cols[4].write(f"**Repro**: {'✓' if ps_r.has_steps_to_reproduce else '✗'}")
        st.markdown("---")

        if mode == "Edit":
            edited_ps = st.text_area(
                "Problem Statement",
                value=inst.get("problem_statement", ""),
                height=200,
                key=f"edit_ps_{selected_id}",
            )
            if st.button("Save", key=f"save_ps_{selected_id}"):
                save_edit(dataset_key, selected_id, {"problem_statement": edited_ps})
                st.success("Saved")
                st.rerun()

            # LLM Fix inline
            _render_llm_fix(selected_id, inst, "problem_statement")
        else:
            st.markdown(inst.get("problem_statement", "*empty*"))

    # ── Gold Patch ────────────────────────────────────────────────────
    with st.expander("Gold Patch", expanded=False):
        st.write(f"**Files**: {pa_r.stats['files']} | **+{pa_r.stats['additions']}/-{pa_r.stats['deletions']}** | **Total**: {pa_r.stats['total_lines']} lines")
        if pa_r.stats["files_list"]:
            st.write("Files: " + ", ".join(f"`{f}`" for f in pa_r.stats["files_list"]))
        st.code(inst.get("patch", ""), language="diff")

    # ── Mask Patch ────────────────────────────────────────────────────
    if inst.get("mask_patch"):
        with st.expander("Mask Patch", expanded=False):
            st.code(inst["mask_patch"], language="diff")

    # ── Test Patch ────────────────────────────────────────────────────
    with st.expander("Test Patch", expanded=False):
        st.write(f"**Test functions**: {len(te_r.test_names)} | **F2P**: {te_r.f2p_count} | **P2P**: {te_r.p2p_count}")
        if te_r.test_names:
            st.write("Tests: " + ", ".join(f"`{t}`" for t in te_r.test_names[:20]))

        if mode == "Edit":
            test_content, test_filename = extract_test_content(inst.get("test_patch", ""))
            if test_content:
                edited_test = st.text_area(
                    "Test Content",
                    value=test_content,
                    height=400,
                    key=f"edit_test_{selected_id}",
                )
                if st.button("Save Test", key=f"save_test_{selected_id}"):
                    new_patch = test_content_to_patch(edited_test, test_filename)
                    new_ids = extract_test_ids(edited_test, test_filename)
                    save_edit(dataset_key, selected_id, {
                        "test_patch": new_patch,
                        "FAIL_TO_PASS": json.dumps(new_ids),
                    })
                    st.success("Saved")
                    st.rerun()
            else:
                st.code(inst.get("test_patch", ""), language="diff")

            # LLM Fix inline
            _render_llm_fix(selected_id, inst, "test_files")
        else:
            st.code(inst.get("test_patch", ""), language="diff")

    # ── F2P / P2P lists ───────────────────────────────────────────────
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

    # ── Test quality (LLM verdict) ────────────────────────────────────
    tq = inst.get("test_quality")
    if tq:
        with st.expander("Test Quality (LLM Verdict)", expanded=False):
            st.json(tq)

    # ── Hints ─────────────────────────────────────────────────────────
    hints = inst.get("hints_text", "")
    if hints:
        with st.expander("Hints Text", expanded=False):
            st.markdown(hints)

    # ── Function metadata ─────────────────────────────────────────────
    fm = inst.get("function_metadata")
    if fm:
        with st.expander("Function Metadata", expanded=False):
            st.json(fm)

    # ── Dockerfile ────────────────────────────────────────────────────
    docker_content = inst.get("dockerfile") or inst.get("docker_template")
    if docker_content:
        with st.expander("Dockerfile", expanded=False):
            st.code(docker_content, language="dockerfile")

    # ── Eval script ───────────────────────────────────────────────────
    if inst.get("eval_script"):
        with st.expander("Eval Script", expanded=False):
            st.code(inst["eval_script"], language="bash")
