"""forecast_engine_v7 실행 러너"""
import sys, subprocess, os

os.chdir(os.path.dirname(__file__))
result = subprocess.run(
    [r"e:\git-copilot\.conda\python.exe", "forecast_engine_v7.py"],
    capture_output=True, text=True, encoding="utf-8"
)

# stdout만 저장 (databricks 로그 제외)
out_lines = [l for l in result.stdout.splitlines()
             if not l.startswith(("INFO:databricks", "ERROR:databricks", "WARNING:databricks", "INFO:root", "ERROR:root"))]
with open("_fe_v7_result.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(out_lines))

# stderr 에러 저장
with open("_fe_v7_err.txt", "w", encoding="utf-8") as f:
    f.write(result.stderr)

print("완료 → _fe_v7_result.txt")
print(f"stdout {len(out_lines)}줄, returncode={result.returncode}")

# 에러 빠른 확인
real_err = [l for l in result.stderr.splitlines()
            if not l.startswith(("INFO:", "ERROR:databricks", "WARNING:", "ERROR:root"))]
if real_err:
    print("=== 에러 (처음 20줄) ===")
    for l in real_err[:20]:
        print(l)
