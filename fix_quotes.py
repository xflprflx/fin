import re

with open('index.html', 'r', encoding='utf-8') as f:
    content = f.read()

# Replace encodeURIComponent with base64 to avoid quote escaping issues
content = content.replace(
    "const rowData = encodeURIComponent(JSON.stringify(r));",
    "const rowData = btoa(unescape(encodeURIComponent(JSON.stringify(r))));"
)

content = content.replace(
    "const data = JSON.parse(decodeURIComponent(dataStr));",
    "const data = JSON.parse(decodeURIComponent(escape(atob(dataStr))));"
)

with open('index.html', 'w', encoding='utf-8') as f:
    f.write(content)
