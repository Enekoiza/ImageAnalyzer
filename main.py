"""ImageAnalyzer — anotador de imágenes por puntos de coordenadas.

Aplicación de escritorio multiplataforma (Windows / macOS) construida con
PyQt6. Permite cargar una imagen, registrar puntos haciendo clic sobre
ella (mapeados a la resolución original), eliminarlos y exportarlos a CSV.

Punto de entrada único: ejecutar `python main.py`.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd
from PIL import Image, ImageDraw, ImageFont

from drone import build_drone_fix, lonlat_to_pixel, pixel_to_lonlat, same_zone
from PyQt6.QtCore import QObject, QPointF, QSize, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QImage, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressDialog,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

# Formatos de imagen aceptados en el diálogo de apertura.
IMAGE_FILTER = "Images (*.jpg *.jpeg *.png *.tif *.tiff)"
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".tif", ".tiff")

# Columnas base de los ficheros de exportación (coordenadas del mundo real).
CORE_EXPORT_COLUMNS = [
    "id", "image", "latitude", "longitude",
    "status", "specie", "review_later", "notes",
]
# Columnas de coordenadas de imagen (X/Y en píxeles respecto a la imagen V),
# añadidas por FileDoctor y por las nuevas exportaciones.
PIXEL_COLUMNS = ["pixel_x", "pixel_y"]
# Columnas (y orden) completas de un fichero de exportación reparado.
EXPORT_COLUMNS = CORE_EXPORT_COLUMNS + PIXEL_COLUMNS


def _load_font(size: int) -> "ImageFont.FreeTypeFont | ImageFont.ImageFont":
    """Carga una fuente TrueType del tamaño pedido; cae a la por defecto.

    Intenta varias fuentes habituales en Windows/macOS/Linux. Si ninguna está
    disponible, usa la fuente bitmap por defecto de Pillow (tamaño fijo).
    """
    for name in ("DejaVuSans-Bold.ttf", "arialbd.ttf", "Arial Bold.ttf", "Arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _app_dir() -> Path:
    """Carpeta del ejecutable (empaquetado) o del script (desarrollo)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_path(relative: str) -> Path:
    """Ruta a un recurso empaquetado (PyInstaller lo extrae en ``_MEIPASS``)."""
    base = Path(getattr(sys, "_MEIPASS", _app_dir()))
    return base / relative


# Fichero de registro de errores, junto al ejecutable / script.
ERROR_LOG = _app_dir() / "ErrorLogs.txt"
# Icono de la aplicación.
APP_ICON = resource_path("assets/logo.png")


def log_error(message: str) -> None:
    """Añade una entrada con fecha al fichero ``ErrorLogs.txt``.

    Crea el fichero si no existe y, si ya existe, añade una nueva línea con
    la marca temporal y el mensaje del fallo.
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(ERROR_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"[{timestamp}] {message}\n")
    except OSError:
        # No interrumpir la aplicación si el log no se puede escribir.
        pass

# Apariencia de los puntos dibujados en el overlay.
# Se dibuja un anillo (sin relleno) para que el centro exacto del clic
# quede visible y rodeado por un círculo.
POINT_RADIUS = 7           # radio del anillo
POINT_RING_WIDTH = 2       # grosor del trazo del anillo
POINT_RING = QColor(255, 0, 0)            # anillo rojo del punto anotado
POINT_RING_SELECTED = QColor(255, 0, 0)   # anillo rojo si está seleccionado
POINT_HALO = QColor(0, 0, 0, 160)         # fino contorno oscuro para contraste

# Tags GeoTIFF (según la especificación GeoTIFF / OGC).
TAG_MODEL_PIXEL_SCALE = 33550
TAG_MODEL_TIEPOINT = 33922
TAG_MODEL_TRANSFORMATION = 34264


@dataclass
class Point:
    """Un record anotado, almacenado en coordenadas del mundo real.

    El record pertenece a la imagen concreta sobre la que se hizo clic
    (``image``) y solo se dibuja en esa imagen.
    """

    id: int
    wx: float            # coordenada de mundo X (lon, o píxel-x si no hay georref)
    wy: float            # coordenada de mundo Y (lat, o píxel-y)
    status: str = ""     # "Individual" | "Nest"
    specie: str = ""     # "Lesser Black-Backed Gull" | "Herring Gull"
    review_later: bool = False  # marcado para revisar más tarde
    notes: str = ""      # notas libres (solo en la exportación)
    image: str = ""      # imagen sobre la que se hizo clic (para el overlay)
    image_v: str = ""    # imagen Vertical de la escena (nombre para el export)
    decimals: int = 0    # decimales con los que mostrar wx/wy en la tabla
    pixel_x: int | None = None  # X en píxeles respecto a la imagen V (export)
    pixel_y: int | None = None  # Y en píxeles respecto a la imagen V (export)


def map_display_to_original(
    display_pos: QPointF,
    pixmap_rect_size: tuple[int, int],
    pixmap_offset: tuple[int, int],
    original_size: tuple[int, int],
) -> tuple[int, int] | None:
    """Convierte una posición de clic en el widget a coordenadas originales.

    La imagen se muestra escalada (manteniendo proporciones) y centrada
    dentro del área de visualización, por lo que un clic en el widget no
    corresponde directamente a un píxel de la imagen original. Esta función
    deshace ese escalado y desplazamiento.

    Args:
        display_pos: Posición del clic en coordenadas del widget (píxeles).
        pixmap_rect_size: Tamaño (ancho, alto) del pixmap *escalado* tal y
            como se muestra en pantalla.
        pixmap_offset: Desplazamiento (x, y) de la esquina superior izquierda
            del pixmap escalado dentro del widget (por el centrado).
        original_size: Resolución (ancho, alto) de la imagen original.

    Returns:
        Tupla (x, y) en coordenadas de la imagen original, o ``None`` si el
        clic cae fuera del área ocupada por la imagen escalada.
    """
    disp_w, disp_h = pixmap_rect_size
    off_x, off_y = pixmap_offset
    orig_w, orig_h = original_size

    if disp_w <= 0 or disp_h <= 0:
        return None

    # Posición relativa al pixmap escalado.
    rel_x = display_pos.x() - off_x
    rel_y = display_pos.y() - off_y

    # Descartar clics fuera de la imagen.
    if rel_x < 0 or rel_y < 0 or rel_x > disp_w or rel_y > disp_h:
        return None

    # Reescalar a la resolución original.
    orig_x = int(round(rel_x / disp_w * orig_w))
    orig_y = int(round(rel_y / disp_h * orig_h))

    # Acotar a los límites válidos por seguridad frente a redondeos.
    orig_x = max(0, min(orig_w - 1, orig_x))
    orig_y = max(0, min(orig_h - 1, orig_y))
    return orig_x, orig_y


class _Affine:
    """Transformación afín píxel↔mundo, con su inversa.

    ``wx = a·px + b·py + c`` ; ``wy = d·px + e·py + f``
    """

    def __init__(self, a, b, c, d, e, f) -> None:
        self._coef = (a, b, c, d, e, f)

    def __call__(self, px: float, py: float) -> tuple[float, float]:
        a, b, c, d, e, f = self._coef
        return a * px + b * py + c, d * px + e * py + f

    def inverse(self, wx: float, wy: float) -> tuple[float, float]:
        a, b, c, d, e, f = self._coef
        det = a * e - b * d
        if det == 0:
            raise ValueError("Transformación afín no invertible")
        px = (e * (wx - c) - b * (wy - f)) / det
        py = (-d * (wx - c) + a * (wy - f)) / det
        return px, py


def build_geotransform(pil_image: Image.Image) -> _Affine | None:
    """Construye la transformación afín píxel↔mundo de un GeoTIFF.

    Lee los tags GeoTIFF estándar para obtener la transformación que convierte
    píxel ``(columna, fila)`` en coordenadas del mundo real (CRS del fichero).
    Se admiten dos representaciones:

    * ``ModelTransformation`` (tag 34264): una matriz afín 4×4 explícita.
    * ``ModelTiepoint`` (tag 33922) + ``ModelPixelScale`` (tag 33550): un
      punto de anclaje píxel↔mundo más la escala por píxel.

    Returns:
        Un :class:`_Affine` (invocable como ``f(px, py)`` y con ``.inverse``),
        o ``None`` si la imagen no está georreferenciada.
    """
    tags = getattr(pil_image, "tag_v2", None)
    if not tags:
        return None

    # Caso 1: matriz de transformación afín explícita (4×4, en orden por filas).
    if TAG_MODEL_TRANSFORMATION in tags:
        m = list(tags[TAG_MODEL_TRANSFORMATION])
        if len(m) == 16:
            return _Affine(m[0], m[1], m[3], m[4], m[5], m[7])

    # Caso 2: punto de anclaje + escala de píxel.
    if TAG_MODEL_TIEPOINT in tags and TAG_MODEL_PIXEL_SCALE in tags:
        tie = list(tags[TAG_MODEL_TIEPOINT])
        scale = list(tags[TAG_MODEL_PIXEL_SCALE])
        if len(tie) >= 6 and len(scale) >= 2:
            i, j, _k, world_x, world_y, _z = tie[:6]
            sx, sy = scale[0], scale[1]
            # wx = world_x + (px - i)·sx ; wy = world_y - (py - j)·sy
            return _Affine(sx, 0.0, world_x - i * sx, 0.0, -sy, world_y + j * sy)

    return None


class ImageView(QLabel):
    """Área de visualización de la imagen con un overlay de puntos.

    Mantiene el pixmap original intacto y compone, en cada repintado, una
    copia escalada con los puntos dibujados encima. La imagen original
    nunca se modifica.
    """

    def __init__(self, on_click) -> None:
        super().__init__()
        self._on_click = on_click
        self._original: QPixmap | None = None
        self._original_size: tuple[int, int] = (0, 0)
        self._geotransform = None  # función píxel→mundo (GeoTIFF) o None.
        self._drone_fix = None     # metadatos de foto de dron o None.
        self._mode = "pixel"       # 'geotiff' | 'drone' | 'pixel'
        self._status_message = "No image loaded"

        # Geometría del renderizado (zoom/pan), usada para mapear clics.
        # _scale = píxeles de pantalla por píxel original; _offset = posición
        # en el widget del píxel original (0, 0).
        self._scale: float = 1.0
        self._offset = QPointF(0.0, 0.0)
        self._needs_fit = True      # recalcular el ajuste en el próximo pintado
        self._user_zoomed = False   # ¿el usuario ha hecho zoom/pan manual?
        self._panning = False
        self._pan_last = QPointF(0.0, 0.0)

        self._points: list[Point] = []
        self._selected_id: int | None = None

        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(400, 400)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setStyleSheet("background-color: #2b2b2b;")
        self.setText("Load an image to start")
        # Sin menú contextual: el clic derecho se usa para desplazar (pan).
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)

    def load_image(self, path: str) -> None:
        """Carga la imagen desde disco usando Pillow y la prepara."""
        with Image.open(path) as img:
            # Determinar la fuente de coordenadas antes de convertir.
            self._geotransform = build_geotransform(img)
            self._drone_fix = None
            if self._geotransform is not None:
                self._mode = "geotiff"
                self._status_message = (
                    "Georeferenced GeoTIFF: X/Y are real-world coordinates "
                    "(file CRS)."
                )
            else:
                self._drone_fix = build_drone_fix(img)
                self._configure_drone_mode()

            rgba = img.convert("RGBA")
            self._original_size = (rgba.width, rgba.height)
            data = rgba.tobytes("raw", "RGBA")
            qimage = QImage(
                data, rgba.width, rgba.height, QImage.Format.Format_RGBA8888
            )
            # copy() para no depender del buffer `data` tras salir del with.
            self._original = QPixmap.fromImage(qimage.copy())

        self.setText("")
        # Resetear zoom/pan al cargar una imagen nueva.
        self._needs_fit = True
        self._user_zoomed = False
        self.update()

    def clear_image(self) -> None:
        """Vacía el visor (p. ej. cuando la imagen no se puede abrir)."""
        self._original = None
        self._original_size = (0, 0)
        self._mode = "pixel"
        self._needs_fit = True
        self._user_zoomed = False
        self.setText("Could not display the image")
        self.update()

    def crop_around(self, px: int, py: int, half: int = 140) -> QPixmap:
        """Devuelve un recorte de la imagen original alrededor de (px, py).

        El recorte cubre la zona clicada y un poco más (``half`` píxeles a cada
        lado) y lleva marcado el punto exacto del clic con un anillo rojo.
        """
        if self._original is None:
            return QPixmap()
        orig_w, orig_h = self._original_size
        x0 = max(0, px - half)
        y0 = max(0, py - half)
        x1 = min(orig_w, px + half)
        y1 = min(orig_h, py + half)
        crop = self._original.copy(x0, y0, x1 - x0, y1 - y0)

        painter = QPainter(crop)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        center = QPointF(px - x0, py - y0)
        painter.setPen(QPen(POINT_HALO, 3))
        painter.drawEllipse(center, 6, 6)
        painter.setPen(QPen(POINT_RING_SELECTED, 2))
        painter.drawEllipse(center, 6, 6)
        painter.end()
        return crop

    def _configure_drone_mode(self) -> None:
        """Decide el modo según los metadatos de dron disponibles."""
        fix = self._drone_fix
        if fix is not None and fix.can_project:
            self._mode = "drone"
            alt = fix.rel_altitude
            self._status_message = (
                f"Drone photo · nadir projection to Lon/Lat (approx., flat "
                f"terrain). Flight altitude: {alt:.1f} m."
            )
            if not fix.is_nadir:
                self._status_message += (
                    f" ⚠ Tilted camera (pitch {fix.pitch_deg:.0f}°): the flat "
                    "projection is NOT reliable for oblique shots."
                )
        elif fix is not None:
            # Hay GPS de cámara pero faltan datos para proyectar por píxel.
            self._mode = "pixel"
            self._status_message = (
                f"Photo with camera GPS (lat {fix.lat:.6f}, lon {fix.lon:.6f}) "
                "but without focal/sensor/altitude to project per pixel. "
                "X/Y will be pixels."
            )
        else:
            self._mode = "pixel"
            self._status_message = (
                "Image without georeference or GPS: X/Y are pixels of the "
                "original image."
            )

    def pixel_to_world(self, px: int, py: int) -> tuple[float, float]:
        """Convierte píxel a coordenada de mundo según el modo activo.

        * ``geotiff``: aplica la transformación afín del GeoTIFF.
        * ``drone``: proyecta a (longitud, latitud) con el modelo nadir.
        * ``pixel``: devuelve las propias coordenadas de píxel.
        """
        if self._mode == "geotiff":
            return self._geotransform(px, py)
        if self._mode == "drone":
            return pixel_to_lonlat(self._drone_fix, px, py)
        return float(px), float(py)

    def world_to_pixel(self, wx: float, wy: float) -> tuple[float, float] | None:
        """Inverso de :meth:`pixel_to_world`: mundo → píxel en esta imagen.

        Permite dibujar una ubicación del mundo real en esta imagen aunque el
        clic se hiciera en su pareja V/T. Devuelve ``None`` si no hay imagen.
        """
        if self._original is None:
            return None
        try:
            if self._mode == "geotiff":
                return self._geotransform.inverse(wx, wy)
            if self._mode == "drone":
                return lonlat_to_pixel(self._drone_fix, wx, wy)
        except (ValueError, ZeroDivisionError):
            return None
        return float(wx), float(wy)

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def drone_fix(self):
        return self._drone_fix

    @property
    def status_message(self) -> str:
        return self._status_message

    def coordinate_meta(self) -> tuple[str, str, str, str, int]:
        """Devuelve (etiqueta_X, etiqueta_Y, csv_X, csv_Y, decimales) del modo."""
        if self._mode == "geotiff":
            return ("X (world)", "Y (world)", "x", "y", 3)
        if self._mode == "drone":
            return ("Longitude", "Latitude", "lon", "lat", 7)
        return ("X (px)", "Y (px)", "x", "y", 0)

    def set_points(self, points: list[Point], selected_id: int | None) -> None:
        """Actualiza la lista de puntos y el punto resaltado, y redibuja."""
        self._points = points
        self._selected_id = selected_id
        self.update()

    @property
    def original_size(self) -> tuple[int, int]:
        return self._original_size

    def _fit_scale(self) -> float:
        """Escala que ajusta la imagen completa al widget (sin distorsión)."""
        orig_w, orig_h = self._original_size
        if not orig_w or not orig_h:
            return 1.0
        return min(self.width() / orig_w, self.height() / orig_h)

    def _fit_to_window(self) -> None:
        """Centra la imagen ajustada al tamaño del widget."""
        orig_w, orig_h = self._original_size
        self._scale = self._fit_scale()
        self._offset = QPointF(
            (self.width() - orig_w * self._scale) / 2.0,
            (self.height() - orig_h * self._scale) / 2.0,
        )

    @property
    def _scaled_size(self) -> tuple[float, float]:
        orig_w, orig_h = self._original_size
        return (orig_w * self._scale, orig_h * self._scale)

    @property
    def _offset_tuple(self) -> tuple[float, float]:
        return (self._offset.x(), self._offset.y())

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if self._original is None:
            super().paintEvent(event)
            return

        if self._needs_fit:
            self._fit_to_window()
            self._needs_fit = False

        orig_w, orig_h = self._original_size
        disp_w = orig_w * self._scale
        disp_h = orig_h * self._scale
        scaled = self._original.scaled(
            max(1, round(disp_w)),
            max(1, round(disp_h)),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

        painter = QPainter(self)
        painter.drawPixmap(self._offset, scaled)

        # Overlay de records: cada uno se proyecta de mundo→píxel en ESTA
        # imagen, así un punto clicado en la pareja V/T aparece en ambas.
        orig_w, orig_h = self._original_size
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for point in self._points:
            pixel = self.world_to_pixel(point.wx, point.wy)
            if pixel is None:
                continue
            px, py = pixel
            if not (0 <= px <= orig_w and 0 <= py <= orig_h):
                continue  # fuera del encuadre de esta imagen
            cx = self._offset.x() + px * self._scale
            cy = self._offset.y() + py * self._scale
            center = QPointF(cx, cy)
            selected = point.id == self._selected_id
            ring = POINT_RING_SELECTED if selected else POINT_RING

            painter.setPen(QPen(POINT_HALO, POINT_RING_WIDTH + 2))
            painter.drawEllipse(center, POINT_RADIUS, POINT_RADIUS)
            painter.setPen(QPen(ring, POINT_RING_WIDTH))
            painter.drawEllipse(center, POINT_RADIUS, POINT_RADIUS)
        painter.end()

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        # Reajustar solo si el usuario no ha hecho zoom/pan manual.
        if self._original is not None and not self._user_zoomed:
            self._needs_fit = True
        super().resizeEvent(event)

    def wheelEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if self._original is None:
            return
        steps = event.angleDelta().y() / 120.0  # un "click" de rueda = 120
        if steps == 0:
            return
        factor = 1.2 ** steps
        # Limitar el zoom entre 0.5× del ajuste y 40 px de pantalla por píxel.
        min_scale = self._fit_scale() * 0.5
        new_scale = max(min_scale, min(40.0, self._scale * factor))
        if new_scale == self._scale:
            return

        # Mantener fijo el punto de la imagen bajo el cursor.
        cursor = event.position()
        orig_x = (cursor.x() - self._offset.x()) / self._scale
        orig_y = (cursor.y() - self._offset.y()) / self._scale
        self._scale = new_scale
        self._offset = QPointF(
            cursor.x() - orig_x * new_scale,
            cursor.y() - orig_y * new_scale,
        )
        self._user_zoomed = True
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if self._original is None:
            return
        button = event.button()
        if button == Qt.MouseButton.LeftButton:
            coords = map_display_to_original(
                event.position(),
                self._scaled_size,
                self._offset_tuple,
                self._original_size,
            )
            if coords is not None:
                self._on_click(coords[0], coords[1])
        elif button in (Qt.MouseButton.MiddleButton, Qt.MouseButton.RightButton):
            # Arrastrar para desplazar (pan) cuando hay zoom.
            self._panning = True
            self._pan_last = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if self._panning:
            delta = event.position() - self._pan_last
            self._pan_last = event.position()
            self._offset += delta
            self._user_zoomed = True
            self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if self._panning and event.button() in (
            Qt.MouseButton.MiddleButton, Qt.MouseButton.RightButton
        ):
            self._panning = False
            self.unsetCursor()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 (Qt override)
        # Doble clic: restablecer el ajuste a ventana.
        if self._original is not None:
            self._needs_fit = True
            self._user_zoomed = False
            self.update()


@dataclass
class VerifyResult:
    """Resultado de comprobar si una imagen tiene la metadata necesaria."""

    path: str
    ok: bool          # ¿se puede procesar (coordenadas de mundo real)?
    mode: str         # 'geotiff' | 'drone' | 'pixel' | 'error'
    reason: str       # explicación legible
    fix: object = None  # DroneFix asociado (para emparejar por GPS) o None


def verify_image(path: str) -> VerifyResult:
    """Verifica si una imagen contiene la metadata necesaria para procesarla.

    Una imagen es **procesable** si permite obtener coordenadas del mundo real:
    bien por ser un GeoTIFF georreferenciado, bien por ser una foto de dron con
    GPS + altura + focal/sensor suficientes para la proyección. En cualquier
    otro caso se considera no procesable (solo daría coordenadas de píxel).

    Args:
        path: Ruta de la imagen a verificar.

    Returns:
        Un :class:`VerifyResult` describiendo el modo y el motivo.
    """
    try:
        with Image.open(path) as img:
            if build_geotransform(img) is not None:
                return VerifyResult(path, True, "geotiff", "GeoTIFF georreferenciado")
            fix = build_drone_fix(img)
    except Exception as exc:  # noqa: BLE001 (imagen corrupta o ilegible)
        return VerifyResult(path, False, "error", f"No se pudo abrir: {exc}")

    if fix is None:
        return VerifyResult(
            path, False, "pixel", "Sin GPS/EXIF ni georreferencia"
        )
    if not fix.can_project:
        missing = [
            name
            for name, value in (
                ("altura", fix.rel_altitude),
                ("focal", fix.focal_mm),
                ("sensor", fix.sensor_w_mm),
            )
            if not value
        ]
        return VerifyResult(
            path, False, "pixel", f"Faltan metadatos: {', '.join(missing)}"
        )
    if not fix.is_nadir:
        return VerifyResult(
            path, True, "drone",
            f"OK · cámara oblicua (pitch {fix.pitch_deg:.0f}°), poco fiable",
            fix=fix,
        )
    return VerifyResult(
        path, True, "drone", "Foto de dron con metadata completa", fix=fix
    )


def _thumbnail_qimage(path: str, size: int = 56) -> QImage | None:
    """Genera una miniatura ``QImage`` de la imagen (apto para hilos de fondo).

    Se usa ``QImage`` (no ``QPixmap``) porque puede construirse fuera del hilo
    de la GUI; el hilo principal la convierte luego a ``QPixmap``.
    """
    try:
        with Image.open(path) as img:
            thumb = img.convert("RGBA")
            thumb.thumbnail((size, size))
            data = thumb.tobytes("raw", "RGBA")
            return QImage(
                data, thumb.width, thumb.height, QImage.Format.Format_RGBA8888
            ).copy()
    except Exception:  # noqa: BLE001
        return None


class _VerifyWorker(QObject):
    """Verifica una lista de imágenes en un hilo aparte para no bloquear la GUI."""

    progress = pyqtSignal(int, object, object)  # índice, VerifyResult, QImage|None
    finished = pyqtSignal()

    def __init__(self, paths: list[str]) -> None:
        super().__init__()
        self._paths = paths
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        for index, path in enumerate(self._paths):
            if self._cancelled:
                break
            result = verify_image(path)
            thumb = _thumbnail_qimage(path)
            self.progress.emit(index, result, thumb)
        self.finished.emit()


@dataclass
class _ImageEntry:
    """Estado por imagen cargada: solo el resultado de la verificación.

    Los puntos anotados NO se guardan aquí: viven en una única lista global en
    :class:`MainWindow`, compartida por todas las imágenes.
    """

    result: VerifyResult
    thumb: object = None  # QImage miniatura para la lista


def _split_suffix(path: str) -> tuple[str, str]:
    """Separa el nombre base y el tipo (`V`/`T`) a partir del sufijo del fichero.

    ``CAM_..._0160_V.JPG`` → (``CAM_..._0160``, ``V``). Si no acaba en ``_V``
    ni ``_T``, devuelve (stem, "").
    """
    stem = Path(path).stem
    if len(stem) > 2 and stem[-2] == "_" and stem[-1] in ("V", "T"):
        return stem[:-2], stem[-1]
    return stem, ""


@dataclass
class _Scene:
    """Una escena a mostrar: imagen primaria (V) y, si existe, su pareja (T)."""

    key: str                      # identificador estable (nombre base)
    label: str                    # texto mostrado en la lista
    left_path: str                # imagen principal (Vertical o única)
    right_path: str | None = None  # pareja (Termal) o None
    ok: bool = True               # ¿al menos la principal es procesable?
    processed: bool = False       # marcada como procesada por el usuario


class AnnotationDialog(QDialog):
    """Diálogo de nuevo registro: recorte de la zona + Status + Specie.

    Muestra una imagen ampliada de la zona clicada y permite elegir el estado
    (Individual / Nest) y la especie (Lesser Black-Backed Gull / Herring Gull).
    El registro solo se confirma al pulsar «Guardar».
    """

    STATUS_OPTIONS = ("Individual", "Nest")
    SPECIE_OPTIONS = ("Lesser Black-Backed Gull", "Herring Gull")

    def __init__(self, crop: QPixmap, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("New record")
        self.setModal(True)

        # Recorte ampliado de la zona clicada.
        image_label = QLabel()
        image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if not crop.isNull():
            image_label.setPixmap(crop.scaled(
                300, 300,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))
        image_label.setStyleSheet("background-color: #2b2b2b;")
        image_label.setMinimumSize(300, 300)

        # Grupo Status.
        self._status_buttons = [QRadioButton(text) for text in self.STATUS_OPTIONS]
        self._status_buttons[0].setChecked(True)
        status_layout = QVBoxLayout()
        for btn in self._status_buttons:
            status_layout.addWidget(btn)
        status_box = QGroupBox("Status")
        status_box.setLayout(status_layout)

        # Grupo Specie.
        self._specie_buttons = [QRadioButton(text) for text in self.SPECIE_OPTIONS]
        self._specie_buttons[0].setChecked(True)
        specie_layout = QVBoxLayout()
        for btn in self._specie_buttons:
            specie_layout.addWidget(btn)
        specie_box = QGroupBox("Specie")
        specie_box.setLayout(specie_layout)

        # Flag "Review Later".
        self._review_later = QCheckBox("Review Later")

        # Notas libres (no se muestran en la tabla, sí en la exportación).
        self._notes = QPlainTextEdit()
        self._notes.setPlaceholderText("Notes…")
        self._notes.setFixedHeight(70)

        # Botones Guardar / Cancelar.
        buttons = QDialogButtonBox()
        save_btn = buttons.addButton("Save", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
        save_btn.setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout()
        layout.addWidget(image_label)
        layout.addWidget(status_box)
        layout.addWidget(specie_box)
        layout.addWidget(self._review_later)
        layout.addWidget(QLabel("Notes"))
        layout.addWidget(self._notes)
        layout.addWidget(buttons)
        self.setLayout(layout)

    def selected_status(self) -> str:
        for btn in self._status_buttons:
            if btn.isChecked():
                return btn.text()
        return self.STATUS_OPTIONS[0]

    def selected_specie(self) -> str:
        for btn in self._specie_buttons:
            if btn.isChecked():
                return btn.text()
        return self.SPECIE_OPTIONS[0]

    def review_later(self) -> bool:
        return self._review_later.isChecked()

    def notes(self) -> str:
        return self._notes.toPlainText().strip()


class MainWindow(QMainWindow):
    """Ventana principal: lista de imágenes · visor · panel de puntos."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("ImageAnalyzer — Point annotator")
        if APP_ICON.exists():
            self.setWindowIcon(QIcon(str(APP_ICON)))
        self.resize(1280, 760)

        # Estado por imagen (solo verificación), indexado por ruta absoluta.
        self._entries: dict[str, _ImageEntry] = {}
        self._scenes: list[_Scene] = []
        self._scene_by_key: dict[str, _Scene] = {}
        self._current_scene: _Scene | None = None

        # Tabla única y global: todos los records de todas las imágenes.
        self._records: list[Point] = []
        self._next_id = 1

        # --- Panel izquierdo: lista de escenas (parejas V/T) a procesar ---
        load_img_btn = QPushButton("Load image")
        load_img_btn.clicked.connect(self._load_image)
        load_folder_btn = QPushButton("Load folder")
        load_folder_btn.clicked.connect(self._load_folder)
        clear_btn = QPushButton("Clear images")
        clear_btn.clicked.connect(self._clear_images)

        self._image_list = QListWidget()
        self._image_list.setIconSize(QSize(40, 40))
        self._image_list.currentItemChanged.connect(self._on_image_selected)

        self._processed_btn = QPushButton("Mark as processed → next")
        self._processed_btn.clicked.connect(self._mark_processed)

        left_header = QHBoxLayout()
        left_header.addWidget(QLabel("Images to process"))
        left_header.addStretch()
        left_collapse = QPushButton("◀")
        left_collapse.setFixedWidth(28)
        left_collapse.setToolTip("Collapse panel")
        left_collapse.clicked.connect(lambda: self._toggle_left(False))
        left_header.addWidget(left_collapse)

        left_layout = QVBoxLayout()
        left_layout.addLayout(left_header)
        left_layout.addWidget(load_img_btn)
        left_layout.addWidget(load_folder_btn)
        left_layout.addWidget(clear_btn)
        left_layout.addWidget(self._image_list, stretch=1)
        self._left_panel = QWidget()
        self._left_panel.setLayout(left_layout)
        self._left_panel.setFixedWidth(230)

        # Tira fina para volver a expandir el panel izquierdo.
        self._left_expand = QPushButton("▶")
        self._left_expand.setFixedWidth(22)
        self._left_expand.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding
        )
        self._left_expand.setToolTip("Expand images panel")
        self._left_expand.clicked.connect(lambda: self._toggle_left(True))
        self._left_expand.hide()

        # --- Centro: dos visores lado a lado (Vertical y Termal) ---
        self._view_v = ImageView(lambda px, py: self._add_point(self._view_v, px, py))
        self._view_t = ImageView(lambda px, py: self._add_point(self._view_t, px, py))
        self._title_v = QLabel("Vertical")
        self._title_t = QLabel("Thermal")
        for title in (self._title_v, self._title_t):
            title.setAlignment(Qt.AlignmentFlag.AlignCenter)
            title.setStyleSheet("font-weight: bold; color: #444;")

        v_layout = QVBoxLayout()
        v_layout.addWidget(self._title_v)
        v_layout.addWidget(self._view_v, stretch=1)
        v_box = QWidget()
        v_box.setLayout(v_layout)

        t_layout = QVBoxLayout()
        t_layout.addWidget(self._title_t)
        t_layout.addWidget(self._view_t, stretch=1)
        self._t_box = QWidget()
        self._t_box.setLayout(t_layout)

        images_layout = QHBoxLayout()
        images_layout.setContentsMargins(0, 0, 0, 0)
        images_layout.addWidget(v_box, stretch=1)
        images_layout.addWidget(self._t_box, stretch=1)
        images_row = QWidget()
        images_row.setLayout(images_layout)

        # Botón "Mark as processed" en el bloque central, abajo y sin ocupar
        # todo el ancho.
        processed_row = QHBoxLayout()
        processed_row.addStretch()
        processed_row.addWidget(self._processed_btn)
        processed_row.addStretch()

        center_layout = QVBoxLayout()
        center_layout.addWidget(images_row, stretch=1)
        center_layout.addLayout(processed_row)
        center_panel = QWidget()
        center_panel.setLayout(center_layout)

        # --- Panel derecho: tabla + botones ---
        self._table = QTableWidget(0, 6)
        self._table.setHorizontalHeaderLabels(
            ["ID", "X", "Y", "Status", "Specie", "Review Later"]
        )
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._table.itemSelectionChanged.connect(self._on_selection_changed)
        self._table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )

        self._delete_btn = QPushButton("Delete record")
        self._delete_btn.clicked.connect(self._delete_selected)
        self._delete_btn.setEnabled(False)

        # Estado de "fichero de export existente cargado".
        self._loaded_export_path: str | None = None
        self._loaded_export_df = None  # filas ya existentes en el fichero

        self._load_export_btn = QPushButton("Load Existing Export File")
        self._load_export_btn.clicked.connect(self._load_existing_export)

        self._export_btn = QPushButton("Export")
        self._export_btn.clicked.connect(self._export)

        self._save_loaded_btn = QPushButton("Save to the loaded file")
        self._save_loaded_btn.clicked.connect(self._save_to_loaded)
        self._save_loaded_btn.hide()

        export_row = QHBoxLayout()
        export_row.addWidget(self._load_export_btn)
        export_row.addWidget(self._export_btn)
        export_row.addWidget(self._save_loaded_btn)

        self._geo_status = QLabel("No image loaded")
        self._geo_status.setWordWrap(True)
        self._geo_status.setStyleSheet("color: #666; font-size: 11px;")

        right_header = QHBoxLayout()
        right_collapse = QPushButton("▶")
        right_collapse.setFixedWidth(28)
        right_collapse.setToolTip("Collapse panel")
        right_collapse.clicked.connect(lambda: self._toggle_right(False))
        right_header.addWidget(right_collapse)
        right_header.addWidget(QLabel("Records"))
        right_header.addStretch()

        right_layout = QVBoxLayout()
        right_layout.addLayout(right_header)
        right_layout.addWidget(self._geo_status)
        right_layout.addWidget(self._table, stretch=1)
        right_layout.addWidget(self._delete_btn)
        right_layout.addLayout(export_row)
        self._right_panel = QWidget()
        self._right_panel.setLayout(right_layout)
        self._right_panel.setFixedWidth(440)

        # Tira fina para volver a expandir el panel derecho.
        self._right_expand = QPushButton("◀")
        self._right_expand.setFixedWidth(22)
        self._right_expand.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding
        )
        self._right_expand.setToolTip("Expand records panel")
        self._right_expand.clicked.connect(lambda: self._toggle_right(True))
        self._right_expand.hide()

        # --- Composición principal ---
        layout = QHBoxLayout()
        layout.addWidget(self._left_expand)
        layout.addWidget(self._left_panel)
        layout.addWidget(center_panel, stretch=1)
        layout.addWidget(self._right_panel)
        layout.addWidget(self._right_expand)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

    # ------------------------------------------------------------------ #
    # Colapsar / expandir paneles laterales
    # ------------------------------------------------------------------ #
    def _toggle_left(self, expand: bool) -> None:
        self._left_panel.setVisible(expand)
        self._left_expand.setVisible(not expand)

    def _toggle_right(self, expand: bool) -> None:
        self._right_panel.setVisible(expand)
        self._right_expand.setVisible(not expand)

    # ------------------------------------------------------------------ #
    # Carga de imágenes
    # ------------------------------------------------------------------ #
    def _load_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open image", "", IMAGE_FILTER)
        if path:
            self._add_paths([path])

    def _load_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Open image folder")
        if not folder:
            return
        paths = sorted(
            str(p)
            for p in Path(folder).iterdir()
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS
        )
        if not paths:
            QMessageBox.warning(
                self, "Empty folder", "No images were found in the folder."
            )
            return
        self._add_paths(paths)

    def _clear_images(self) -> None:
        """Vacía la lista de imágenes cargadas y los visores.

        Los records ya anotados se conservan en la tabla (son el resultado);
        solo se descartan las imágenes y su verificación.
        """
        self._entries.clear()
        self._scenes = []
        self._scene_by_key = {}
        self._current_scene = None
        self._image_list.clear()
        self._view_v.clear_image()
        self._view_t.clear_image()
        self._title_v.setText("Vertical")
        self._title_t.setText("Thermal")
        self._t_box.setVisible(True)
        self._geo_status.setText("No image loaded")
        self._refresh()

    def _add_paths(self, paths: list[str]) -> None:
        """Verifica las imágenes nuevas (en segundo plano si son varias)."""
        new_paths = [p for p in paths if p not in self._entries]
        if not new_paths:
            return
        if len(new_paths) == 1:
            # Una sola imagen: verificación instantánea, sin diálogo.
            path = new_paths[0]
            result = verify_image(path)
            self._register_image(path, result, _thumbnail_qimage(path))
            self._rebuild_scenes()
            if not result.ok:
                self._show_unprocessable([Path(path).name])
            self._select_first_scene_if_needed()
            return
        self._start_async_verify(new_paths)

    def _register_image(self, path: str, result: VerifyResult, thumb=None) -> None:
        """Guarda la entrada de la imagen y registra el error si lo hay."""
        if not result.ok:
            log_error(f"{path} — not processable: {result.reason}")
        self._entries[path] = _ImageEntry(result=result, thumb=thumb)

    # ------------------------------------------------------------------ #
    # Escenas (emparejado V/T por nombre + verificación de zona por GPS)
    # ------------------------------------------------------------------ #
    def _same_zone(self, path_v: str, path_t: str) -> bool:
        """¿La V y la T cubren la misma zona (según sus coordenadas GPS)?"""
        fix_v = self._entries[path_v].result.fix
        fix_t = self._entries[path_t].result.fix
        if fix_v is None or fix_t is None:
            return False
        return same_zone(fix_v, fix_t)

    def _rebuild_scenes(self) -> None:
        """Agrupa las imágenes en escenas (parejas V/T) y repuebla la lista."""
        groups: dict[str, dict] = {}
        for path in self._entries:
            base, kind = _split_suffix(path)
            group = groups.setdefault(base, {"V": None, "T": None, "other": []})
            if kind == "V":
                group["V"] = path
            elif kind == "T":
                group["T"] = path
            else:
                group["other"].append(path)

        def ok(path: str) -> bool:
            return self._entries[path].result.ok

        scenes: list[_Scene] = []
        for base, group in groups.items():
            v, t = group["V"], group["T"]
            if v and t and self._same_zone(v, t):
                scenes.append(_Scene(base, base, v, t, ok(v) or ok(t)))
            else:
                if v:
                    scenes.append(_Scene(f"{base}_V", f"{base}_V", v, None, ok(v)))
                if t:
                    scenes.append(_Scene(f"{base}_T", f"{base}_T", t, None, ok(t)))
            for other in group["other"]:
                stem = Path(other).stem
                scenes.append(_Scene(stem, Path(other).name, other, None, ok(other)))

        # Conservar el estado "processed" de escenas previas con la misma clave.
        was_processed = {s.key for s in self._scenes if s.processed}
        for scene in scenes:
            if scene.key in was_processed:
                scene.processed = True

        scenes.sort(key=lambda s: s.label)
        self._scenes = scenes
        self._scene_by_key = {s.key: s for s in scenes}
        self._repopulate_list()

    def _repopulate_list(self) -> None:
        """Vuelca las escenas en el QListWidget, preservando la selección."""
        current_key = self._current_scene.key if self._current_scene else None
        self._image_list.blockSignals(True)
        self._image_list.clear()
        for scene in self._scenes:
            self._image_list.addItem(self._make_scene_item(scene))
        self._image_list.blockSignals(False)
        # Restaurar la selección por clave si la escena sigue existiendo.
        if current_key is not None:
            for row in range(self._image_list.count()):
                if self._image_list.item(row).data(Qt.ItemDataRole.UserRole) == current_key:
                    self._image_list.setCurrentRow(row)
                    break

    def _make_scene_item(self, scene: _Scene) -> QListWidgetItem:
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, scene.key)
        entry = self._entries.get(scene.left_path)
        if entry is not None:
            item.setToolTip(f"{entry.result.mode.upper()} — {entry.result.reason}")
            if entry.thumb is not None:
                item.setIcon(QIcon(QPixmap.fromImage(entry.thumb).scaled(
                    56, 56,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )))
        self._style_scene_item(item, scene)
        return item

    def _style_scene_item(self, item: QListWidgetItem, scene: _Scene) -> None:
        """Aplica texto, color y fondo al item según estado (ok / procesada)."""
        mark = "✓" if scene.ok else "✗"
        pair = " · V+T" if scene.right_path else ""
        item.setText(f"{mark} {scene.label}{pair}")
        # Procesada: fondo verde (sin texto extra). Si no, fondo normal.
        item.setBackground(QColor(200, 235, 200) if scene.processed else QColor(0, 0, 0, 0))
        if not scene.ok:
            item.setForeground(QColor(200, 40, 40))
        else:
            item.setForeground(QColor(0, 0, 0))

    def _mark_processed(self) -> None:
        """Marca la escena activa como procesada y salta a la siguiente SIN procesar."""
        if self._current_scene is None:
            return
        self._current_scene.processed = True
        row = self._image_list.currentRow()
        item = self._image_list.item(row)
        if item is not None:
            self._style_scene_item(item, self._current_scene)
        # Buscar la siguiente imagen no procesada (recorre toda la lista en bucle).
        count = self._image_list.count()
        for offset in range(1, count + 1):
            idx = (row + offset) % count
            key = self._image_list.item(idx).data(Qt.ItemDataRole.UserRole)
            scene = self._scene_by_key.get(key)
            if scene is not None and not scene.processed:
                self._image_list.setCurrentRow(idx)
                return
        # Si todas están procesadas, no se mueve.

    def _select_first_scene_if_needed(self) -> None:
        if self._current_scene is None and self._image_list.count():
            self._image_list.setCurrentRow(0)

    # ------------------------------------------------------------------ #
    # Verificación en segundo plano (carpetas con muchas imágenes)
    # ------------------------------------------------------------------ #
    def _start_async_verify(self, paths: list[str]) -> None:
        self._unprocessable: list[str] = []

        self._progress = QProgressDialog(
            "Validating image metadata…", "Cancel", 0, len(paths), self
        )
        self._progress.setWindowTitle("Loading")
        self._progress.setWindowModality(Qt.WindowModality.WindowModal)
        self._progress.setMinimumDuration(0)
        self._progress.setValue(0)

        self._verify_thread = QThread()
        self._verify_worker = _VerifyWorker(paths)
        self._verify_worker.moveToThread(self._verify_thread)
        self._verify_thread.started.connect(self._verify_worker.run)
        self._verify_worker.progress.connect(self._on_verify_progress)
        self._verify_worker.finished.connect(self._on_verify_finished)
        self._progress.canceled.connect(self._verify_worker.cancel)
        self._verify_thread.start()

    def _on_verify_progress(self, index: int, result: VerifyResult, thumb) -> None:
        self._register_image(result.path, result, thumb)
        if not result.ok:
            self._unprocessable.append(Path(result.path).name)
        self._progress.setValue(index + 1)

    def _on_verify_finished(self) -> None:
        self._progress.setValue(self._progress.maximum())
        self._verify_thread.quit()
        self._verify_thread.wait()
        self._rebuild_scenes()
        self._select_first_scene_if_needed()
        if self._unprocessable:
            self._show_unprocessable(self._unprocessable)

    def _show_unprocessable(self, names: list[str]) -> None:
        """Muestra una ventana con los nombres de las imágenes no procesables."""
        listing = "\n".join(f"• {n}" for n in names)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Unprocessable images")
        box.setText(
            f"{len(names)} image(s) without the required metadata "
            "(logged in ErrorLogs.txt):"
        )
        if len(names) > 20:
            box.setInformativeText("Click “Show Details” to see the list.")
            box.setDetailedText(listing)
        else:
            box.setInformativeText(listing)
        box.exec()

    # ------------------------------------------------------------------ #
    # Cambio de escena activa
    # ------------------------------------------------------------------ #
    def _on_image_selected(
        self, current: QListWidgetItem | None, _previous: QListWidgetItem | None
    ) -> None:
        if current is None:
            return
        key = current.data(Qt.ItemDataRole.UserRole)
        scene = self._scene_by_key.get(key)
        if scene is None:
            return
        self._current_scene = scene

        self._load_into(self._view_v, self._title_v, scene.left_path, "Vertical")
        if scene.right_path:
            self._t_box.setVisible(True)
            self._load_into(self._view_t, self._title_t, scene.right_path, "Thermal")
        else:
            self._t_box.setVisible(False)
            self._view_t.clear_image()

        x_label, y_label, _cx, _cy, _dec = self._view_v.coordinate_meta()
        self._table.setHorizontalHeaderLabels(
            ["ID", x_label, y_label, "Status", "Specie", "Review Later"]
        )
        self._geo_status.setText(self._view_v.status_message)
        self._refresh()

    def _load_into(
        self, view: ImageView, title: QLabel, path: str, kind: str
    ) -> None:
        """Carga ``path`` en ``view`` y actualiza su título."""
        entry = self._entries.get(path)
        name = Path(path).name
        if entry is not None and entry.result.mode == "error":
            view.clear_image()
            title.setText(f"{kind} — could not open")
            return
        try:
            view.load_image(path)
            title.setText(f"{kind} — {name}")
        except Exception as exc:  # noqa: BLE001
            log_error(f"{path} — error while loading: {exc}")
            view.clear_image()
            title.setText(f"{kind} — error")

    # ------------------------------------------------------------------ #
    # Anotación
    # ------------------------------------------------------------------ #
    def _add_point(self, view: ImageView, px: int, py: int) -> None:
        if self._current_scene is None:
            return
        # Imagen concreta sobre la que se hizo clic (V o T de la escena).
        image_path = (
            self._current_scene.left_path
            if view is self._view_v
            else self._current_scene.right_path
        )
        if image_path is None:
            return
        # Abrir el diálogo con el recorte de la zona; solo se guarda al aceptar.
        crop = view.crop_around(px, py)
        dialog = AnnotationDialog(crop, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        wx, wy = view.pixel_to_world(px, py)
        _xl, _yl, _cx, _cy, decimals = view.coordinate_meta()
        # Coordenadas de imagen respecto a la V (la que figura en el export).
        # Si el clic fue en la V coincide con (px, py); si fue en la T se
        # reproyecta sobre la V. Si cae fuera del encuadre de la V, se deja
        # vacío: no se inventan píxeles negativos.
        pixel_x = pixel_y = None
        pixel_xy = self._view_v.world_to_pixel(wx, wy)
        if pixel_xy is not None:
            vw, vh = self._view_v.original_size
            if -1.0 <= pixel_xy[0] <= vw and -1.0 <= pixel_xy[1] <= vh:
                pixel_x = max(0, min(vw - 1, int(round(pixel_xy[0]))))
                pixel_y = max(0, min(vh - 1, int(round(pixel_xy[1]))))
        self._records.append(Point(
            self._next_id, wx, wy,
            dialog.selected_status(), dialog.selected_specie(),
            review_later=dialog.review_later(), notes=dialog.notes(),
            image=image_path, image_v=self._current_scene.left_path,
            decimals=decimals, pixel_x=pixel_x, pixel_y=pixel_y,
        ))
        self._next_id += 1
        self._refresh()

    def _delete_selected(self) -> None:
        point_id = self._selected_point_id()
        if point_id is None:
            return
        self._records = [p for p in self._records if p.id != point_id]
        self._refresh()

    def _records_dataframe(self) -> "pd.DataFrame":
        """DataFrame de los records de la tabla, con las columnas de export."""
        return pd.DataFrame(
            [
                {
                    "id": p.id,
                    "image": Path(p.image_v).name,
                    "latitude": p.wy,
                    "longitude": p.wx,
                    "status": p.status,
                    "specie": p.specie,
                    "review_later": p.review_later,
                    "notes": p.notes,
                    "pixel_x": p.pixel_x,
                    "pixel_y": p.pixel_y,
                }
                for p in self._records
            ],
            columns=EXPORT_COLUMNS,
        )

    @staticmethod
    def _write_dataframe(frame: "pd.DataFrame", path: str) -> None:
        """Escribe el DataFrame a CSV o XLSX según la extensión de ``path``."""
        if path.lower().endswith(".xlsx"):
            frame.to_excel(path, index=False)
        else:
            frame.to_csv(path, index=False)

    @staticmethod
    def _draw_points_on_image(
        img_path: str, points: list[Point], photos_dir: Path
    ) -> None:
        """Guarda en ``photos_dir`` una copia de la imagen con sus puntos en rojo.

        Junto a cada círculo se escribe el ``id`` del record (el mismo que
        figura en el CSV) para poder identificarlo visualmente.
        """
        with Image.open(img_path) as img:
            canvas = img.convert("RGB")
        draw = ImageDraw.Draw(canvas)
        # Radio/grosor proporcionales al tamaño para que se vea en fotos grandes.
        radius = max(8, round(min(canvas.size) * 0.006))
        width = max(2, round(radius / 3))
        # Fuente para el id, escalada al tamaño de la imagen.
        font = _load_font(max(14, round(min(canvas.size) * 0.014)))
        for p in points:
            if p.pixel_x is None or p.pixel_y is None:
                continue
            x, y = int(p.pixel_x), int(p.pixel_y)
            draw.ellipse(
                [x - radius, y - radius, x + radius, y + radius],
                outline=(255, 0, 0), width=width,
            )
            # Id pegado al círculo, arriba a la derecha.
            draw.text(
                (x + radius + 2, y - radius - 2), str(p.id),
                fill=(255, 0, 0), font=font, anchor="lb",
            )
        canvas.save(photos_dir / Path(img_path).name)

    def _render_annotated_photos(self, photos_dir: Path) -> int:
        """Escribe en ``photos_dir`` cada imagen V anotada con sus puntos en rojo.

        Agrupa los records por su imagen V (la que figura en el CSV) y dibuja un
        círculo rojo en cada (pixel_x, pixel_y). Devuelve cuántas fotos se han
        escrito. Se omiten los records sin coordenadas de imagen o cuya imagen
        ya no esté en disco.
        """
        by_image: dict[str, list[Point]] = {}
        for p in self._records:
            if p.image_v:
                by_image.setdefault(p.image_v, []).append(p)

        photos_dir.mkdir(parents=True, exist_ok=True)
        saved = 0
        for img_path, points in by_image.items():
            if not Path(img_path).exists():
                log_error(f"{img_path} — photo not found, skipped in export")
                continue
            try:
                self._draw_points_on_image(img_path, points, photos_dir)
                saved += 1
            except Exception as exc:  # noqa: BLE001 (imagen ilegible/corrupta)
                log_error(f"{img_path} — error rendering annotated photo: {exc}")
        return saved

    def _export(self) -> None:
        """Exporta el resultado a una carpeta: ``records.csv`` + ``photos/``.

        La carpeta de salida contiene el CSV con los records (incluyendo las
        coordenadas de imagen ``pixel_x``/``pixel_y``) y una subcarpeta
        ``photos`` con cada imagen V anotada y sus puntos dibujados en rojo.
        """
        if not self._records:
            QMessageBox.warning(
                self, "No data", "There are no records to export."
            )
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export results (choose a folder name)", "results",
        )
        if not path:
            return

        # El usuario elige el nombre/ubicación de la carpeta de resultados.
        out_dir = Path(path)
        if out_dir.suffix:
            out_dir = out_dir.with_suffix("")
        csv_path = out_dir / "records.csv"
        photos_dir = out_dir / "photos"

        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            self._write_dataframe(self._records_dataframe(), str(csv_path))
            photos = self._render_annotated_photos(photos_dir)
        except (OSError, ValueError) as exc:
            log_error(f"{out_dir} — error while exporting: {exc}")
            QMessageBox.critical(
                self, "Error", f"Could not save the results:\n{exc}"
            )
            return
        QMessageBox.information(
            self, "Exported",
            f"Exported {len(self._records)} records to {out_dir.name}/"
            f"records.csv and {photos} annotated photo(s) to "
            f"{out_dir.name}/photos.",
        )

    def _load_existing_export(self) -> None:
        """Carga un fichero de export existente para continuar añadiendo records.

        La tabla queda vacía, pero al guardar los nuevos records se añadirán
        DESPUÉS de la última fila del fichero cargado. El botón Export pasa a
        ser «Save to the loaded file».
        """
        path, _ = QFileDialog.getOpenFileName(
            self, "Load existing export file", "",
            "Export files (*.csv *.xlsx)",
        )
        if not path:
            return
        try:
            existing = (
                pd.read_excel(path) if path.lower().endswith(".xlsx")
                else pd.read_csv(path)
            )
        except (OSError, ValueError) as exc:
            log_error(f"{path} — error while loading export: {exc}")
            QMessageBox.critical(self, "Error", f"Could not read the file:\n{exc}")
            return

        # Validar las columnas: se acepta tanto el formato completo (con
        # pixel_x/pixel_y) como el antiguo (solo coordenadas del mundo real).
        cols = list(existing.columns)
        if cols not in (EXPORT_COLUMNS, CORE_EXPORT_COLUMNS):
            QMessageBox.critical(
                self, "Invalid file",
                "The file must have the same columns as an export:\n"
                + ", ".join(CORE_EXPORT_COLUMNS)
                + "\n(optionally followed by: " + ", ".join(PIXEL_COLUMNS) + ")",
            )
            return

        # Normalizar al formato completo (añade pixel_x/pixel_y vacías si faltan).
        existing = existing.reindex(columns=EXPORT_COLUMNS)
        self._loaded_export_path = path
        self._loaded_export_df = existing
        # La tabla queda vacía; los nuevos records continúan tras el último id.
        self._records = []
        try:
            self._next_id = int(existing["id"].max()) + 1 if len(existing) else 1
        except (ValueError, TypeError):
            self._next_id = len(existing) + 1
        self._refresh()

        # Export -> Save to the loaded file.
        self._export_btn.hide()
        self._save_loaded_btn.show()
        self._geo_status.setText(
            f"Loaded export file: {Path(path).name} "
            f"({len(existing)} existing records). New records will be appended."
        )

    def _save_to_loaded(self) -> None:
        """Guarda los records de la tabla tras la última fila del fichero cargado."""
        if self._loaded_export_path is None:
            return
        if not self._records:
            QMessageBox.warning(
                self, "No data", "There are no new records to save."
            )
            return
        combined = pd.concat(
            [self._loaded_export_df, self._records_dataframe()], ignore_index=True
        )
        # Las fotos anotadas se guardan en un subdirectorio junto al CSV cargado.
        photos_dir = Path(self._loaded_export_path).parent / "photos"
        try:
            self._write_dataframe(combined, self._loaded_export_path)
            photos = self._render_annotated_photos(photos_dir)
        except (OSError, ValueError) as exc:
            log_error(f"{self._loaded_export_path} — error while saving: {exc}")
            QMessageBox.critical(
                self, "Error", f"Could not save the file:\n{exc}"
            )
            return
        QMessageBox.information(
            self, "Saved",
            f"Saved {len(self._records)} new records to "
            f"{Path(self._loaded_export_path).name} "
            f"(after {len(self._loaded_export_df)} existing rows) and "
            f"{photos} annotated photo(s) to photos/.",
        )

    # ------------------------------------------------------------------ #
    # Estado de la interfaz
    # ------------------------------------------------------------------ #
    def _on_selection_changed(self) -> None:
        selected_id = self._selected_point_id()
        self._delete_btn.setEnabled(selected_id is not None)
        self._redraw_overlay(selected_id)

    def _selected_point_id(self) -> int | None:
        items = self._table.selectedItems()
        if not items:
            return None
        row = items[0].row()
        id_item = self._table.item(row, 0)
        return int(id_item.text()) if id_item else None

    def _redraw_overlay(self, selected_id: int | None) -> None:
        """Cada visor dibuja solo los records hechos sobre su propia imagen."""
        scene = self._current_scene
        left = scene.left_path if scene else None
        right = scene.right_path if scene else None
        self._view_v.set_points(
            [p for p in self._records if p.image == left], selected_id
        )
        self._view_t.set_points(
            [p for p in self._records if p.image == right], selected_id
        )

    def _refresh(self) -> None:
        """Reconstruye la tabla global y redibuja el overlay de la imagen activa."""
        selected_id = self._selected_point_id()

        self._table.blockSignals(True)
        self._table.setRowCount(len(self._records))
        for row, p in enumerate(self._records):
            self._table.setItem(row, 0, QTableWidgetItem(str(p.id)))
            self._table.setItem(row, 1, QTableWidgetItem(_format_coord(p.wx, p.decimals)))
            self._table.setItem(row, 2, QTableWidgetItem(_format_coord(p.wy, p.decimals)))
            self._table.setItem(row, 3, QTableWidgetItem(p.status))
            self._table.setItem(row, 4, QTableWidgetItem(p.specie))
            self._table.setItem(
                row, 5, QTableWidgetItem("Yes" if p.review_later else "")
            )
        self._table.blockSignals(False)

        # Mantener la selección si el record sigue existiendo.
        if selected_id is not None and any(p.id == selected_id for p in self._records):
            for row, p in enumerate(self._records):
                if p.id == selected_id:
                    self._table.selectRow(row)
                    break
        else:
            selected_id = None
            self._delete_btn.setEnabled(False)

        self._redraw_overlay(selected_id)


def _format_coord(value: float, decimals: int) -> str:
    """Formatea una coordenada con el número de decimales del modo (0 = entero)."""
    if decimals <= 0:
        return str(int(round(value)))
    return f"{value:.{decimals}f}"


def main() -> int:
    app = QApplication(sys.argv)
    if APP_ICON.exists():
        app.setWindowIcon(QIcon(str(APP_ICON)))
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
