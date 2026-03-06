# Verify the generated docker file and test files with gold patch F2P check

python3 evaluation/run_evaluation.py \
    --dataset_name "/home/yuansui/swe-factory-dev/internal-swe-bench-data/results_v1_gpt_5_2_40_20260306.json" \
    --predictions_path "gold" \
    --is_judge_fail2pass \
    --run_id "all_f2p" \
    --output_path "run_instances"