"""Create one offline, sendable HTML with the complete student archive inside."""
import base64
import gzip
import json
import re
from pathlib import Path


def inline_trajectory(page: str, asset_folder: Path | None = None, *, blob: bool = False) -> str:
    """Make the trace viewer an actual self-contained HTML document.

    Maintainer source may reference companion assets. Student packages contain
    the inlined result, so subsequent bundling never needs those source files.
    """
    if '<link rel="stylesheet" href="trajectory.css">' in page:
        if asset_folder is None:
            raise ValueError("Trajectory CSS source folder is required")
        page = page.replace('<link rel="stylesheet" href="trajectory.css">',
                            '<style>' + (asset_folder / "trajectory.css").read_text(encoding="utf-8") + '</style>')
    for name in ("trajectory-data.js", "trajectory.js"):
        reference = f'<script src="{name}"></script>'
        if reference in page:
            if asset_folder is None:
                raise ValueError("Trajectory script source folder is required")
            script = (asset_folder / name).read_text(encoding="utf-8")
            page = page.replace(reference, '<script>' + re.sub(
                r'</script', lambda _: '<\\/script', script, flags=re.IGNORECASE) + '</script>')
    if blob:
        # A blob document cannot change its URL to a relative query URL.
        page = page.replace("history.replaceState(null,'','?case='+selected);", "")
        page = page.replace('<a href="../index.html#offline">← К заданию</a>',
                            '<button onclick="window.close()">← Закрыть траекторию</button>')
    return page


def build_standalone(folder: Path) -> Path:
    page = (folder / "index.html").read_text(encoding="utf-8")
    # The repository entry offers this generated HTML; the generated page itself
    # must offer the ZIP, never recursively embed its previous build.
    page = page.replace('href="homework.html" download>Скачать ДЗ одним HTML',
                        'href="homework.zip" download>Скачать ДЗ целиком')
    assets = {}
    for reference in dict.fromkeys(re.findall(r'href="([^"#][^"]*)"', page)):
        if ":" in reference:
            continue
        path = folder / reference
        if not path.is_file():
            raise FileNotFoundError(reference)
        content = path.read_bytes()
        mime = "application/octet-stream"
        view = False
        if reference == "_support/trajectory.html":
            viewer = inline_trajectory(content.decode("utf-8"), blob=True)
            content = viewer.encode("utf-8")
            mime = "text/html;charset=utf-8"
            view = True
        packed = reference != "homework.zip"
        assets[reference] = {
            "data": base64.b64encode(gzip.compress(content, mtime=0) if packed else content).decode("ascii"),
            "gzip": packed, "mime": mime, "view": view,
            "filename": path.name,
        }
    page = page.replace("if(location.protocol==='file:')document.getElementById('download-bundle').remove();", "")
    page = page.replace('<a href="runtime/harness_lab/etl_agent.py">', '<a href="runtime/harness_lab/etl_agent.py" download>')
    page = page.replace('<a href="runtime/harness_lab/etl_env.py">', '<a href="runtime/harness_lab/etl_env.py" download>')
    page = page.replace('<a href="runtime/harness_lab/etl_optimizer.py">', '<a href="runtime/harness_lab/etl_optimizer.py" download>')
    page = page.replace('<a href="_support/task_adapter.py">', '<a href="_support/task_adapter.py" download>')
    page = page.replace('<a href="README.md">README задания</a>', '<a href="README.md" download>Скачать README</a>')
    page = page.replace('>Полный код ETL-агента</a>', '>Скачать код ETL-агента</a>')
    page = page.replace('>etl_agent.py</a>', '>Скачать etl_agent.py</a>')
    page = page.replace('>etl_env.py</a>', '>Скачать etl_env.py</a>')
    page = page.replace('>etl_optimizer.py</a>', '>Скачать etl_optimizer.py</a>')
    script = r"""
<script id="embedded-homework" type="application/json">__PAYLOAD__</script>
<script>
(async () => {
  const assets = JSON.parse(document.getElementById('embedded-homework').textContent);
  // Prevent navigation to missing sibling files while assets are being decoded.
  const links = [...document.querySelectorAll('a[href]')].filter(a => assets[a.getAttribute('href')]);
  for (const link of links) {
    link.dataset.asset = link.getAttribute('href');
    link.removeAttribute('href');
    link.setAttribute('aria-disabled', 'true');
  }
  for (const [name, asset] of Object.entries(assets)) {
    let blob = new Blob([Uint8Array.from(atob(asset.data), c => c.charCodeAt(0))]);
    if (asset.gzip) blob = await new Response(blob.stream().pipeThrough(new DecompressionStream('gzip'))).blob();
    const url = URL.createObjectURL(new Blob([blob], {type: asset.mime}));
    for (const link of links.filter(a => a.dataset.asset === name)) {
      link.href = url;
      link.removeAttribute('aria-disabled');
      if (asset.view) { link.target = '_blank'; link.rel = 'noopener'; }
      else { link.download = asset.filename; }
    }
  }
  document.documentElement.dataset.homeworkReady = 'true';
})().catch(error => {
  const message = document.createElement('p');
  message.className = 'note';
  message.textContent = 'Не удалось подготовить встроенные файлы. Откройте HTML в актуальном Chrome, Edge или Firefox. ' + error.message;
  document.querySelector('header').append(message);
});
</script>
""".replace("__PAYLOAD__", json.dumps(assets, ensure_ascii=False, separators=(",", ":")))
    page = page.replace('</body>', script + '</body>')
    output = folder / "homework.html"
    output.write_text(page, encoding="utf-8", newline="\n")
    return output
