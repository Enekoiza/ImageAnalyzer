"""Lectura de metadatos de fotos de dron y proyección píxel→mundo (nadir).

Una foto de dron es un JPG con metadatos:
  * **EXIF**: GPS del centro de la cámara (lat/lon/altitud), focal, sensor.
  * **XMP** (DJI y otros): altura *relativa* de vuelo y orientación (yaw).

A diferencia de un GeoTIFF, una foto **no** contiene una rejilla de
coordenadas por píxel: solo la posición de la cámara. Para estimar la
coordenada de un píxel cualquiera se proyecta asumiendo:

  * cámara apuntando recto hacia abajo (**nadir**), y
  * **terreno plano** a la altura relativa de vuelo.

Es una **aproximación**: ignora el relieve del terreno y la inclinación de
la cámara. Sirve para una estimación rápida, no para topografía de
precisión. Para precisión real hay que generar un ortomosaico
georreferenciado (OpenDroneMap, Pix4D, Agisoft…).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from PIL import Image

# Tags EXIF relevantes.
_EXIF_IFD = 0x8769
_GPS_IFD = 0x8825
_TAG_FOCAL_LENGTH = 37386          # FocalLength (mm)
_TAG_FOCAL_35MM = 41989            # FocalLengthIn35mmFilm
_TAG_FP_X_RES = 41486             # FocalPlaneXResolution
_TAG_FP_Y_RES = 41487             # FocalPlaneYResolution
_TAG_FP_UNIT = 41488              # FocalPlaneResolutionUnit

# Metros por unidad para FocalPlaneResolutionUnit (2=pulgada, 3=cm, 4=mm, 5=µm).
_UNIT_MM = {2: 25.4, 3: 10.0, 4: 1.0, 5: 0.001}

# Aproximación de metros por grado de latitud (suficiente para el rango local).
_METERS_PER_DEG_LAT = 111_320.0


@dataclass
class DroneFix:
    """Metadatos de georreferenciación aproximada de una foto de dron."""

    lat: float           # latitud del centro de cámara (grados)
    lon: float           # longitud del centro de cámara (grados)
    img_w: int
    img_h: int
    rel_altitude: float | None = None  # altura sobre el suelo/despegue (m)
    yaw_deg: float = 0.0               # rumbo de la cámara (horario desde el norte)
    pitch_deg: float | None = None     # inclinación del gimbal (-90 = nadir)
    focal_mm: float | None = None
    sensor_w_mm: float | None = None
    sensor_h_mm: float | None = None

    @property
    def can_project(self) -> bool:
        """¿Hay datos suficientes para proyectar píxel→mundo?"""
        return (
            self.rel_altitude is not None
            and self.focal_mm not in (None, 0)
            and self.sensor_w_mm not in (None, 0)
            and self.sensor_h_mm not in (None, 0)
        )

    @property
    def is_nadir(self) -> bool:
        """¿La cámara apuntaba (casi) recta hacia abajo?

        El modelo de proyección plana solo es razonable para tomas cenitales.
        Si no hay dato de inclinación se asume nadir (no se puede saber).
        """
        if self.pitch_deg is None:
            return True
        return abs(self.pitch_deg + 90.0) <= 10.0


def _to_float(value) -> float | None:
    """Convierte un valor EXIF (IFDRational, tupla o número) a float."""
    if value is None:
        return None
    try:
        if isinstance(value, tuple):  # racional como (num, den)
            return value[0] / value[1]
        return float(value)
    except (TypeError, ZeroDivisionError, ValueError):
        return None


def dms_to_degrees(dms, ref: str | None) -> float | None:
    """Convierte coordenada GPS en grados/minutos/segundos a grados decimales.

    Args:
        dms: Secuencia (grados, minutos, segundos) tal y como la entrega EXIF.
        ref: Referencia cardinal ('N', 'S', 'E', 'W'). Sur y Oeste son negativos.

    Returns:
        Grados decimales con signo, o ``None`` si la entrada no es válida.
    """
    try:
        d = _to_float(dms[0])
        m = _to_float(dms[1])
        s = _to_float(dms[2])
    except (TypeError, IndexError):
        return None
    if d is None or m is None or s is None:
        return None
    deg = d + m / 60.0 + s / 3600.0
    if ref and ref.upper() in ("S", "W"):
        deg = -deg
    return deg


def _sensor_size_mm(
    exif_ifd: dict,
    focal_mm: float | None,
    focal_35mm: float | None,
    img_w: int,
    img_h: int,
) -> tuple[float | None, float | None]:
    """Estima el tamaño físico del sensor (ancho, alto) en milímetros.

    Intenta dos métodos en orden de fiabilidad:
      1. A partir de la resolución del plano focal (tags EXIF FocalPlane*).
      2. A partir de la focal equivalente a 35 mm y la real (factor de recorte).
    """
    fp_x = _to_float(exif_ifd.get(_TAG_FP_X_RES))
    fp_y = _to_float(exif_ifd.get(_TAG_FP_Y_RES))
    unit = exif_ifd.get(_TAG_FP_UNIT)
    if fp_x and fp_y and unit in _UNIT_MM:
        mm = _UNIT_MM[unit]
        # FocalPlaneXResolution = píxeles por unidad → ancho = píxeles / (px/unidad).
        return img_w / fp_x * mm, img_h / fp_y * mm

    if focal_mm and focal_35mm:
        crop = focal_35mm / focal_mm
        diag_35 = math.hypot(36.0, 24.0)  # diagonal de un fotograma completo
        diag = diag_35 / crop
        aspect = img_w / img_h
        sensor_h = diag / math.hypot(aspect, 1.0)
        return sensor_h * aspect, sensor_h

    return None, None


def _parse_xmp(xmp: bytes | str | None) -> dict:
    """Extrae del XMP los campos de georreferenciación de dron disponibles.

    Returns:
        Diccionario con claves opcionales: ``rel_alt``, ``yaw``, ``pitch``,
        ``lat`` y ``lon`` (las que aparezcan en el XMP).
    """
    if not xmp:
        return {}
    if isinstance(xmp, bytes):
        xmp = xmp.decode("utf-8", errors="ignore")

    def _find(key: str) -> float | None:
        # Admite tanto atributos (key="...") como elementos (<key>...</key>).
        match = re.search(rf'{key}[">=<\s]+([+-]?\d+(?:\.\d+)?)', xmp)
        return float(match.group(1)) if match else None

    yaw = _find("GimbalYawDegree")
    if yaw is None:
        yaw = _find("FlightYawDegree")
    return {
        "rel_alt": _find("RelativeAltitude"),
        "yaw": yaw,
        "pitch": _find("GimbalPitchDegree"),
        "lat": _find("GpsLatitude"),
        "lon": _find("GpsLongitude"),
    }


def build_drone_fix(pil_image: Image.Image) -> DroneFix | None:
    """Lee los metadatos de una foto de dron y construye un :class:`DroneFix`.

    Returns:
        Un :class:`DroneFix` si la foto tiene al menos GPS de cámara (su
        ``can_project`` indica si además se puede proyectar por píxel), o
        ``None`` si no hay coordenadas GPS en absoluto.
    """
    exif = pil_image.getexif()
    xmp = _parse_xmp(pil_image.info.get("xmp"))

    # GPS: preferir el del XMP (decimal, más preciso); si no, el EXIF en DMS.
    lat, lon = xmp.get("lat"), xmp.get("lon")
    if lat is None or lon is None:
        gps = exif.get_ifd(_GPS_IFD) if exif else None
        if gps:
            lat = dms_to_degrees(gps.get(2), gps.get(1))
            lon = dms_to_degrees(gps.get(4), gps.get(3))
    if lat is None or lon is None:
        return None

    exif_ifd = exif.get_ifd(_EXIF_IFD) if exif else {}
    focal_mm = _to_float(exif_ifd.get(_TAG_FOCAL_LENGTH))
    focal_35 = _to_float(exif_ifd.get(_TAG_FOCAL_35MM))
    img_w, img_h = pil_image.size
    sensor_w, sensor_h = _sensor_size_mm(exif_ifd, focal_mm, focal_35, img_w, img_h)

    yaw = xmp.get("yaw")
    return DroneFix(
        lat=lat,
        lon=lon,
        img_w=img_w,
        img_h=img_h,
        rel_altitude=xmp.get("rel_alt"),
        yaw_deg=yaw if yaw is not None else 0.0,
        pitch_deg=xmp.get("pitch"),
        focal_mm=focal_mm,
        sensor_w_mm=sensor_w,
        sensor_h_mm=sensor_h,
    )


def pixel_to_lonlat(fix: DroneFix, px: float, py: float) -> tuple[float, float]:
    """Proyecta un píxel a (longitud, latitud) con el modelo nadir/terreno plano.

    Asume cámara cenital y terreno horizontal a ``fix.rel_altitude``. Calcula
    el GSD (metros por píxel) a partir del tamaño del sensor, la focal y la
    altura, desplaza desde el centro de la imagen, rota según el rumbo de la
    cámara (``yaw``) para alinear con el norte y convierte metros a grados.

    Requiere ``fix.can_project``; en caso contrario lanza ``ValueError``.
    """
    if not fix.can_project:
        raise ValueError("DroneFix sin datos suficientes para proyectar")

    gsd_x = fix.sensor_w_mm * fix.rel_altitude / (fix.focal_mm * fix.img_w)
    gsd_y = fix.sensor_h_mm * fix.rel_altitude / (fix.focal_mm * fix.img_h)

    # Desplazamiento en metros desde el centro, en el marco de la imagen.
    dx = (px - fix.img_w / 2.0) * gsd_x   # derecha en la imagen
    dy = (py - fix.img_h / 2.0) * gsd_y   # hacia abajo en la imagen

    # En el marco de la imagen: arriba (-dy) = dirección de avance (heading).
    forward = -dy
    right = dx

    theta = math.radians(fix.yaw_deg)
    east = forward * math.sin(theta) + right * math.cos(theta)
    north = forward * math.cos(theta) - right * math.sin(theta)

    dlat = north / _METERS_PER_DEG_LAT
    dlon = east / (_METERS_PER_DEG_LAT * math.cos(math.radians(fix.lat)))
    return fix.lon + dlon, fix.lat + dlat


def lonlat_to_pixel(fix: DroneFix, lon: float, lat: float) -> tuple[float, float]:
    """Inverso de :func:`pixel_to_lonlat`: proyecta (lon, lat) a píxel.

    Permite dibujar una misma ubicación del mundo real en otra imagen del par
    V/T (que suele tener distinto zoom). Requiere ``fix.can_project``.
    """
    if not fix.can_project:
        raise ValueError("DroneFix sin datos suficientes para proyectar")

    gsd_x = fix.sensor_w_mm * fix.rel_altitude / (fix.focal_mm * fix.img_w)
    gsd_y = fix.sensor_h_mm * fix.rel_altitude / (fix.focal_mm * fix.img_h)

    north = (lat - fix.lat) * _METERS_PER_DEG_LAT
    east = (lon - fix.lon) * _METERS_PER_DEG_LAT * math.cos(math.radians(fix.lat))

    theta = math.radians(fix.yaw_deg)
    # Inverso de la rotación (la matriz es ortogonal y simétrica).
    forward = east * math.sin(theta) + north * math.cos(theta)
    right = east * math.cos(theta) - north * math.sin(theta)

    px = fix.img_w / 2.0 + right / gsd_x
    py = fix.img_h / 2.0 - forward / gsd_y
    return px, py


def footprint_m(fix: DroneFix) -> tuple[float, float] | None:
    """Tamaño de la huella en el suelo (ancho, alto en metros), o ``None``."""
    if not fix.can_project:
        return None
    width = fix.sensor_w_mm * fix.rel_altitude / fix.focal_mm
    height = fix.sensor_h_mm * fix.rel_altitude / fix.focal_mm
    return width, height


def center_distance_m(a: DroneFix, b: DroneFix) -> float:
    """Distancia aproximada (m) entre los centros de cámara de dos imágenes."""
    mean_lat = math.radians((a.lat + b.lat) / 2.0)
    dx = (b.lon - a.lon) * math.cos(mean_lat) * _METERS_PER_DEG_LAT
    dy = (b.lat - a.lat) * _METERS_PER_DEG_LAT
    return math.hypot(dx, dy)


def same_zone(a: DroneFix, b: DroneFix) -> bool:
    """¿Dos imágenes cubren aproximadamente la misma zona del mundo real?

    Se considera que sí cuando la distancia entre sus centros es menor que la
    media-diagonal de la mayor de sus huellas (es decir, hay solape amplio).
    """
    fa, fb = footprint_m(a), footprint_m(b)
    if fa is None or fb is None:
        return center_distance_m(a, b) <= 100.0  # sin huella: umbral fijo
    half_diag = max(math.hypot(*fa), math.hypot(*fb)) / 2.0
    return center_distance_m(a, b) <= half_diag
