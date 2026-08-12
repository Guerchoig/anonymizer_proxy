import urllib.request, json

payload = json.dumps({
    "model": "qwen/qwen-3.7-plus",
    "messages": [{"role": "user", "content": "Привет, меня зовут Иван Петров, работаю в ООО Ромашка"}],
    "stream": True
}).encode()

req = urllib.request.Request(
    "http://localhost:8081/v1/chat/completions",
    data=payload,
    headers={"Content-Type": "application/json"}
)

print("Sending request...")
resp = urllib.request.urlopen(req, timeout=180)
print(f"Status: {resp.status}")
print(f"Content-Type: {resp.headers.get('Content-Type')}")
print("---")
for line in resp:
    decoded = line.decode("utf-8").strip()
    if decoded:
        print(decoded)