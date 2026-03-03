# verify the generated docker file and test files

python3 evaluation/run_evaluation.py \
    --dataset_name "/home/yuansui/swe-factory-dev/internal-swe-bench-data/MiroMindAI__miroflow/setup_output_2026-03-03/results/results.json" \
    --predictions_path "gold" \
    --is_judge_fail2pass \
    --run_id "miroflow_f2p" \
    --output_path "run_instances"

python3 evaluation/run_evaluation.py \
    --dataset_name "/home/yuansui/swe-factory-dev/internal-swe-bench-data/MiroMindAI__MiroThinker/setup_output_2026-03-03/results/results.json" \
    --predictions_path "gold" \
    --is_judge_fail2pass \
    --run_id "mirothinker_f2p" \
    --output_path "run_instances"