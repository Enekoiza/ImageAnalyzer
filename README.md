# ImageAnalyzer

[![Build executables](https://github.com/Enekoiza/ImageAnalyzer/actions/workflows/build.yml/badge.svg)](https://github.com/Enekoiza/ImageAnalyzer/actions/workflows/build.yml)

Aplicación de escritorio multiplataforma (Windows y macOS) para **anotar
imágenes haciendo clic** sobre ellas y registrar puntos de coordenadas.
Cada punto se mapea a la resolución original de la imagen, se muestra en
una tabla lateral y puede exportarse a CSV.

## Descargas

Ejecutables listos para usar (última versión compilada automáticamente):

| Sistema | Descarga |
|---------|----------|
| **Windows** | [ImageAnalyzer-Windows.exe](https://github.com/Enekoiza/ImageAnalyzer/releases/download/latest/ImageAnalyzer-Windows.exe) |
| **macOS** | [ImageAnalyzer-macOS.zip](https://github.com/Enekoiza/ImageAnalyzer/releases/download/latest/ImageAnalyzer-macOS.zip) |

> En macOS, la primera vez ábrela con **clic derecho → Abrir** (app sin firmar).
> Estos enlaces apuntan siempre a la última compilación publicada por CI; si
> aún no existe, lanza el workflow desde la pestaña **Actions**.

## Características

- **Parejas Vertical/Termal**: los ficheros que terminan en `_V` (Vertical) y
  `_T` (Termal) con la misma base de nombre se emparejan automáticamente,
  **verificando además por GPS** que cubren la misma zona. Cada pareja se
  muestra como una sola entrada en la lista y se visualiza **lado a lado**
  (Vertical a la izquierda, Termal a la derecha). Como la termal suele usar más
  zoom, cada imagen se anota de forma independiente (un clic crea **un único
  record en la imagen donde se hace clic**).
- Botón **Clear images** para vaciar la lista de imágenes cargadas y los
  visores (los records ya anotados se conservan en la tabla).
- Botón **Mark as processed → next** que marca la imagen/escena activa como
  procesada (queda señalada en verde con «✅ processed» en la lista) y salta
  automáticamente a la siguiente. El estado se conserva al recargar la lista.
- Interfaz (botones, columnas de la tabla, títulos) en **inglés**.
- Carga de **una imagen** o de una **carpeta completa** de imágenes. Al cargar
  una carpeta, la verificación se hace en **segundo plano** con una **barra de
  progreso** (la app no se bloquea); al terminar, si hay imágenes sin la
  metadata necesaria se muestra una ventana con sus nombres.
- **Zoom con la rueda del ratón** (hacia el cursor) y **desplazamiento (pan)**
  arrastrando con el botón central o derecho. Doble clic restablece el ajuste a
  ventana. El zoom no afecta a la precisión: las coordenadas siempre se calculan
  sobre la resolución original.
- **Lista de imágenes a procesar** en el panel izquierdo, con miniatura y
  marca de estado (✓ procesable / ✗ no procesable). Cada imagen conserva sus
  propios puntos al cambiar entre ellas.
- **Verificación automática de metadata** al cargar: comprueba si cada imagen
  tiene la información necesaria (GeoTIFF georreferenciado o foto de dron con
  GPS + altura + focal/sensor). Las no procesables se marcan en rojo.
- **Registro de errores**: cualquier imagen no procesable o ilegible se anota
  en `ErrorLogs.txt` (junto al ejecutable) con la fecha y el motivo; si el
  fichero ya existe, se añade una nueva entrada.
- Carga de imágenes **JPG, PNG y TIFF**.
- La imagen se muestra escalada **sin distorsión** y la original **nunca
  se modifica**: todo el dibujo ocurre en una capa superpuesta (overlay).
- Clic izquierdo → abre un **diálogo de nuevo registro** que muestra un
  **recorte ampliado** de la zona clicada (con el punto marcado) y permite
  elegir el **Status** (Individual / Nest), la **Specie** (Lesser Black-Backed
  Gull / Herring Gull), una casilla **Review Later** y un campo de **Notes**
  (notas libres). El registro solo se añade al pulsar **Guardar**; al guardarse,
  aparece en la tabla con un **anillo** blanco en la imagen y las columnas
  **Status**, **Specie** y **Review Later**. Las **notas no se muestran en la
  tabla**, pero sí se incluyen como columna en el CSV exportado.
- **Coordenadas del mundo real** — la app detecta automáticamente la fuente
  de coordenadas al cargar la imagen y trabaja en uno de tres modos:

  | Modo | Cuándo | Coordenadas | Precisión |
  |------|--------|-------------|-----------|
  | **GeoTIFF** | La imagen lleva tags GeoTIFF (`ModelPixelScale`/`ModelTiepoint`/`ModelTransformation`) | X/Y en el CRS del fichero (p. ej. UTM) | Exacta |
  | **Dron** | Foto JPG con GPS en EXIF + altura en XMP (DJI…) + focal/sensor | Longitud/Latitud por **proyección nadir** | Aproximada |
  | **Píxel** | Sin georreferencia ni GPS | X/Y en píxeles de la resolución original | — |

  El panel lateral indica en todo momento qué modo está activo.

  > **Sobre el modo Dron (aproximado):** una foto individual solo guarda la
  > posición GPS *de la cámara*, no una coordenada por píxel. La app estima la
  > coordenada de cada clic asumiendo **cámara cenital (nadir)** y **terreno
  > plano** a la altura de vuelo, usando el GSD (metros/píxel) derivado de la
  > focal, el tamaño del sensor y la altura relativa (XMP). Ignora el relieve
  > del terreno y la inclinación de la cámara. Para precisión topográfica hay
  > que generar antes un **ortomosaico** georreferenciado (OpenDroneMap, Pix4D,
  > Agisoft) y cargar ese GeoTIFF en la app.
- **Tabla única y global**: la tabla de la derecha
  (`ID, X, Y, Status, Specie, Review Later`) acumula los records de **todas**
  las imágenes, independientemente de cuál esté seleccionada. Cada record
  recuerda a qué imagen pertenece, y el overlay de cada imagen solo dibuja sus
  propios puntos.
- ID autoincremental **global** por record.
- Selección de un record en la tabla → se **resalta** en la imagen (si
  pertenece a la imagen activa).
- Botón **Eliminar punto** para borrar el punto seleccionado (la tabla y
  el overlay se redibujan automáticamente).
- Botón **Export** que permite elegir formato **CSV** o **Excel (.xlsx)** con
  todos los records globales (`id, image, latitude, longitude, status, specie,
  review_later, notes`); avisa si no hay registros.

## Stack

| Componente        | Tecnología   |
|-------------------|--------------|
| GUI               | PyQt6        |
| Imágenes          | Pillow       |
| Exportación       | pandas       |
| Empaquetado       | PyInstaller  |

## Requisitos

- Python 3.10 o superior.

## Instalación

```bash
# 1. Crear y activar un entorno virtual
python -m venv .venv

# Windows (PowerShell)
.venv\Scripts\Activate.ps1
# macOS / Linux
source .venv/bin/activate

# 2. Instalar dependencias
pip install -r requirements.txt
```

## Ejecución

```bash
python main.py
```

## Uso

1. Pulsa **Cargar imagen** (un fichero) o **Cargar carpeta** (todas las
   imágenes de un directorio). Aparecerán en la lista de la izquierda con su
   estado de verificación; los fallos se registran en `ErrorLogs.txt`.
2. Selecciona una imagen de la lista y haz **clic izquierdo** sobre ella para
   registrar puntos. Usa la **rueda** para hacer zoom y arrastra con el **botón
   central/derecho** para desplazarte; **doble clic** restablece la vista.
3. Selecciona una fila de la tabla para resaltar su punto; usa
   **Eliminar punto** para borrarlo.
4. Pulsa **Exportar** para guardar todos los puntos en un CSV.

## Generar el ejecutable con PyInstaller

PyInstaller produce un binario **específico de cada plataforma**: para
obtener un `.exe` de Windows debes ejecutarlo en Windows, y para un binario
de macOS debes ejecutarlo en macOS.

### Windows

```powershell
pip install pyinstaller
pyinstaller --noconfirm --windowed --onefile --name ImageAnalyzer `
  --icon assets/logo.ico `
  --add-data "assets/logo.png;assets" `
  main.py
```

El ejecutable se genera en `dist\ImageAnalyzer.exe` con el logo como icono.

### macOS

```bash
pip install pyinstaller
pyinstaller --noconfirm --windowed --onefile --name ImageAnalyzer \
  --icon assets/logo.icns \
  --add-data "assets/logo.png:assets" \
  main.py
```

Se genera `dist/ImageAnalyzer` (binario) y `dist/ImageAnalyzer.app` (bundle).

> **Nota sobre el separador de `--add-data`:** Windows usa `;` y macOS/Linux
> usan `:` entre origen y destino (como en los comandos de arriba).

> **Notas**
> - `--windowed` evita que se abra una consola junto a la ventana GUI.
> - `--onefile` empaqueta todo en un único fichero (arranque algo más
>   lento). Omítelo si prefieres una carpeta con arranque más rápido.
> - En macOS, para distribuir fuera de tu equipo necesitarás *firmar* y
>   *notarizar* la app con tu cuenta de desarrollador de Apple.

## Estructura del proyecto

```
ImageAnalyzer/
├── main.py            # Punto de entrada único (GUI + georreferencia GeoTIFF)
├── drone.py           # Lectura EXIF/XMP de dron + proyección nadir píxel→lon/lat
├── assets/
│   ├── logo.png       # Logo / icono de ventana
│   ├── logo.ico       # Icono del ejecutable (Windows)
│   └── logo.icns      # Icono del ejecutable (macOS)
├── requirements.txt   # Dependencias
└── README.md
```
