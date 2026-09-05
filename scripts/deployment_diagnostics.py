"""Print bounded startup logs with container environment values redacted."""
import json
import re
import subprocess

container = "financial-semantic-agent-agent-api-1"
inspection = subprocess.check_output(["docker", "inspect", container], text=True)
config = json.loads(inspection)[0]
values = [entry.partition("=")[2] for entry in config["Config"]["Env"]]
result = subprocess.run(
    ["docker", "logs", "--tail", "80", container],
    text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
)
output = result.stdout
for value in sorted(set(values), key=len, reverse=True):
    if len(value) >= 4:
        output = output.replace(value, "[REDACTED]")
output = re.sub(r"\b\w+://[^\s'\"]+", "[REDACTED_URL]", output)
print(output)
