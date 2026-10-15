"""Build the sendable Jev slides with their code archive embedded.

Run after rebuilding practice/practice.zip when the student code changes.
"""
import base64
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def build() -> Path:
    page = (ROOT / "v2.html").read_text(encoding="utf-8")
    # This distribution offers the embedded code, rather than another copy of itself.
    page = page.replace('<a href="jev-workshop.html" download>Скачать презентацию одним HTML ↓</a>', '')
    page = page.replace('<a href="index.html">Первая версия презентации ↗</a>', '')
    for name in ("styles.css", "v2.css"):
        css = (ROOT / name).read_text(encoding="utf-8")
        if re.search(r"@import|url\(", css):
            raise ValueError(f"External CSS asset needs inlining: {name}")
        page = page.replace(f'<link rel="stylesheet" href="{name}">', f'<style>{css}</style>')
    # Put executable scripts after the DOM. Inline defer scripts execute immediately.
    scripts = []
    for name in ("practice-data.js", "v2.js"):
        source = (ROOT / name).read_text(encoding="utf-8")
        source = re.sub(r"</script", lambda _: r"<\/script", source, flags=re.I)
        page = page.replace(f'<script defer src="{name}"></script>', '')
        scripts.append(f'<script>{source}</script>')
    assets = {}
    for reference in dict.fromkeys(re.findall(r'href="([^"#][^"]*)"', page)):
        if re.match(r"(?:https?://|mailto:)", reference):
            continue
        path = (ROOT / reference).resolve()
        if not path.is_relative_to(ROOT) or not path.is_file():
            raise ValueError(f"Missing local asset: {reference}")
        mime = "application/zip" if path.suffix == ".zip" else "text/plain;charset=utf-8"
        assets[reference] = {"base64": base64.b64encode(path.read_bytes()).decode("ascii"),
                             "mime": mime, "filename": path.name}
        # Every local asset is downloadable from the HTML; there are no local file dependencies.
        page = re.sub(r'<a\b[^>]*href="' + re.escape(reference) + r'"[^>]*>',
                      lambda match: re.sub(r'\s+download(?:="[^"]*")?', '', match[0]).replace(
                          f'href="{reference}"', f'href="#" data-asset="{reference}" download="{path.name}"'), page)
    payload = json.dumps(assets, ensure_ascii=True, separators=(",", ":"))
    downloads = """<script id="embedded-assets" type="application/json">PAYLOAD</script>
<script>
(() => {
  const assets = JSON.parse(document.getElementById('embedded-assets').textContent);
  for (const [name, asset] of Object.entries(assets)) {
    const bytes = Uint8Array.from(atob(asset.base64), c => c.charCodeAt(0));
    const url = URL.createObjectURL(new Blob([bytes], {type: asset.mime}));
    document.querySelectorAll('a[data-asset]').forEach(a => {
      if (a.dataset.asset === name) { a.href = url; a.download = asset.filename; }
    });
  }
  document.documentElement.dataset.standaloneReady = 'true';
})();
</script>""".replace("PAYLOAD", payload)
    page = page.replace('</body>', ''.join(scripts) + downloads + '</body>')
    if re.search(r'<(?:script|link|img)\b[^>]*(?:src|href)="[^#]', page):
        raise ValueError("Non-inline rendering dependency remains")
    output = ROOT / "jev-workshop.html"
    output.write_text(page, encoding="utf-8")
    print(f"{output}: {output.stat().st_size:,} bytes; {len(assets)} embedded downloads")
    return output


if __name__ == "__main__":
    build()
