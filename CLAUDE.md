# CLAUDE.md

Guía para trabajar en este repositorio. **Este proyecto es Python + PyQt6**,
no .NET/React (ignora el stack del CLAUDE.md global para este repo).

## Qué es

**ImageAnalyzer** — aplicación de escritorio multiplataforma (Windows/macOS)
para **anotar puntos sobre imágenes de dron** y registrar avistamientos de
gaviotas. Cada clic crea un *record* con coordenadas del mundo real (lat/lon),
estado (Individual/Nest), especie (Lesser Black-Backed Gull / Herring Gull),
flag "Review Later" y notas. Los records se exportan a CSV/XLSX.

## Stack

| Capa | Tecnología |
|------|------------|
| GUI | PyQt6 |
| Imágenes / EXIF / XMP | Pillow |
| Exportación | pandas + openpyxl (CSV/XLSX) |
| Empaquetado | PyInstaller |

Dependencias en `requirements.txt`. Entorno en `.venv/`.

## Estructura

```
main.py            # Punto de entrada único: GUI, georref GeoTIFF, records, export
drone.py           # EXIF/XMP de dron + proyección nadir píxel↔lon/lat
assets/            # logo.png (ventana), logo.ico (exe Windows), logo.icns (app macOS)
build_macos.sh     # Build del .app en un Mac (PyInstaller no compila cruzado)
.github/workflows/build.yml  # CI: compila Win+macOS y publica Release "latest"
requirements.txt
```

## Conceptos de dominio (clave)

- **Modos de coordenadas** (autodetectados al cargar cada imagen):
  - `geotiff`: imagen georreferenciada → afín píxel↔mundo (`_Affine` en main.py).
  - `drone`: foto JPG con GPS (EXIF) + altura/yaw/pitch (XMP) + focal/sensor →
    **proyección nadir** a lon/lat (`drone.pixel_to_lonlat` / `lonlat_to_pixel`).
    Asume cámara cenital y terreno plano: es **aproximado**.
  - `pixel`: sin metadatos → X/Y en píxeles.
- **Parejas V/T**: ficheros que acaban en `_V` (Vertical) y `_T` (Thermal) con
  la misma base se emparejan en una **escena** (`_Scene`), verificando además
  por GPS que cubren la misma zona (`drone.same_zone`). Se muestran **lado a
  lado**. La termal suele tener más zoom. Un record pertenece SOLO a la imagen
  donde se hizo clic (`Point.image`), pero el export usa el nombre de la V
  (`Point.image_v`).
- **Tabla de records global**: única para todas las imágenes (`_records`,
  `_next_id`). El overlay de cada visor filtra por su imagen.
- **Verificación de metadatos**: al cargar carpeta, `verify_image` valida en un
  hilo de fondo (`_VerifyWorker`) con barra de progreso; las no procesables se
  registran en `ErrorLogs.txt` (`log_error`) y se listan en un diálogo.
- **Export**: columnas `EXPORT_COLUMNS = [id, image, latitude, longitude,
  status, specie, review_later, notes]`. "Load Existing Export File" carga un
  fichero con esas columnas; "Save to the loaded file" reescribe = filas
  existentes + records actuales (idempotente, sin duplicar).

## Componentes UI (main.py / MainWindow)

- **Izquierda**: lista de escenas (colapsable). Procesada = fondo verde
  (`_style_scene_item`). "Mark as processed → next" salta a la siguiente sin
  procesar (está en el bloque central, abajo).
- **Centro**: `ImageView` Vertical + Thermal lado a lado. Zoom con rueda
  (hacia el cursor), pan con botón derecho/central, doble clic = ajustar.
- **Derecha**: tabla de records (colapsable) + Delete / Load Existing Export /
  Export ↔ Save to the loaded file.
- `AnnotationDialog`: recorte ampliado del clic + Status + Specie + Review
  Later + Notes; el record solo se crea al pulsar Save.

## Comandos

```bash
# Ejecutar
.venv/Scripts/python.exe main.py        # Windows (PowerShell: .\.venv\Scripts\Activate.ps1)

# Build local (Windows)
.venv/Scripts/python.exe -m PyInstaller --noconfirm --windowed --onefile \
  --name ImageAnalyzer --icon assets/logo.ico --add-data "assets/logo.png;assets" main.py
```

## Tests (patrón usado en este repo)

No hay suite formal; se valida con scripts puntuales **headless**:

```bash
QT_QPA_PLATFORM=offscreen PYTHONIOENCODING=utf-8 PYTHONUTF8=1 \
  ./.venv/Scripts/python.exe -u _smoke.py
```

Convenciones al testear la GUI:
- `QT_QPA_PLATFORM=offscreen` para correr sin pantalla.
- **Stubear los diálogos modales** (bloquean en headless):
  `QMessageBox.warning/information/critical = staticmethod(lambda *a, **k: None)`
  y `main.AnnotationDialog.exec = lambda self: QDialog.DialogCode.Accepted`.
- Para cargas de carpeta (asíncronas), bombear el bucle:
  `while w._verify_thread.isRunning(): app.processEvents()`.
- `isVisible()` da False sin `show()`; usar `isHidden()` para comprobar botones.
- Borrar el script temporal y `ErrorLogs.txt` al terminar.

## Convenciones de código

- Comentarios/docstrings en **español**; textos de UI en **inglés**.
- Type hints y `from __future__ import annotations`.
- Lógica de mapeo de coordenadas aislada y documentada (main.py y drone.py).
- Commits estilo Conventional Commits (feat/fix/docs/ci/refactor…).

## Notas / limitaciones

- La proyección de dron es **nadir + terreno plano** (aproximada); para
  precisión topográfica se necesita un ortomosaico GeoTIFF.
- PyInstaller **no** compila cruzado: el `.app` de macOS se genera en un Mac o
  vía el workflow de GitHub Actions (Release con tag rodante `latest`).
- `ErrorLogs.txt` se escribe junto al ejecutable/script (`_app_dir`).
