"""
Procreate File Reader
=====================
Reads and parses .procreate files (which are ZIP archives).
Supports Procreate 4.x and 5.x formats.

.procreate files contain:
  - QuickLook/Thumbnail.png  -> Preview thumbnail
  - Document.archive         -> Binary plist with document metadata
  - Layer data in numbered directories

Author: ProcreateViewer (Open Source)
License: MIT
"""

import zipfile
import plistlib
import struct
import io
import os
import re
import subprocess
import tempfile
import json
from typing import Optional, List, Dict, Any

from PIL import Image


class ProcreateLayer:
    """Represents a single layer in a Procreate document."""

    def __init__(
        self,
        name: str = "Untitled",
        uuid: str = "",
        opacity: float = 1.0,
        visible: bool = True,
        blend_mode: int = 0,
        is_folder: bool = False,
        children: Optional[List["ProcreateLayer"]] = None,
        animation_held_length: int = 0,
        is_background_color: bool = False,
        contents_rect: Optional[tuple] = None,
    ):
        self.name = name
        self.uuid = uuid
        self.opacity = opacity
        self.visible = visible
        self.blend_mode = blend_mode
        self.is_folder = is_folder
        self.children: List[ProcreateLayer] = children or []
        self.animation_held_length = max(0, int(animation_held_length))
        self.is_background_color = is_background_color
        self.contents_rect = contents_rect
        self.thumbnail: Optional[Image.Image] = None

    def __repr__(self):
        if self.is_folder:
            return f"ProcreateLayerFolder('{self.name}', {len(self.children)} children)"
        vis = "visible" if self.visible else "hidden"
        return f"ProcreateLayer('{self.name}', {vis}, opacity={self.opacity:.0%})"


class ProcreateFile:
    """
    Parser for .procreate files.

    Usage:
        with ProcreateFile('artwork.procreate') as pf:
            pf.thumbnail.show()
            print(f"Canvas: {pf.canvas_width}x{pf.canvas_height}")
            for layer in pf.layers:
                print(layer)
    """

    BLEND_MODES = {
        0: "Normal",
        1: "Multiply",
        2: "Screen",
        3: "Overlay",
        4: "Darken",
        5: "Lighten",
        6: "Color Dodge",
        7: "Color Burn",
        8: "Soft Light",
        9: "Hard Light",
        10: "Difference",
        11: "Exclusion",
        12: "Hue",
        13: "Saturation",
        14: "Color",
        15: "Luminosity",
        16: "Add",
        17: "Linear Burn",
        18: "Vivid Light",
        19: "Linear Light",
        20: "Pin Light",
        21: "Hard Mix",
        22: "Subtract",
        23: "Divide",
    }

    def __init__(self, filepath: str):
        self.filepath = os.path.abspath(filepath)
        self.filename = os.path.basename(filepath)
        self.thumbnail: Optional[Image.Image] = None
        self.composite: Optional[Image.Image] = None
        self.layers: List[ProcreateLayer] = []
        self.canvas_width: int = 0
        self.canvas_height: int = 0
        self.dpi: int = 132
        self.orientation: int = 0
        self.color_profile: str = "sRGB"
        self.layer_count: int = 0
        self.video_enabled: bool = False
        self.metadata: Dict[str, Any] = {}
        self.layer_tree: List[ProcreateLayer] = []
        self.folder_count: int = 0
        self.animation_assist_enabled: bool = False
        self.animation_frame_count: int = 0
        self.animation_settings: Dict[str, Any] = {}
        self.animation_playback_mode: str = "loop"
        self.animation_playback_direction: str = "forward"
        self.background_color = (255, 255, 255, 255)
        self.background_visible: bool = True
        self.background_layer = ProcreateLayer(
            name="Background Color",
            visible=True,
            is_background_color=True,
        )
        self.archived_videos: List[Dict[str, Any]] = []
        self._zip: Optional[zipfile.ZipFile] = None
        self._document_archive: Optional[Dict] = None
        self._archive_objects: List[Any] = []
        self._load()

    # ── Loading ────────────────────────────────────────────────────────

    def _load(self):
        """Load and parse the .procreate file."""
        if not os.path.isfile(self.filepath):
            raise FileNotFoundError(f"File not found: {self.filepath}")

        if not zipfile.is_zipfile(self.filepath):
            raise ValueError(f"Not a valid .procreate file (not a ZIP): {self.filepath}")

        self._zip = zipfile.ZipFile(self.filepath, "r")
        self._scan_archived_videos()
        self._load_thumbnail()
        self._load_composite()
        self._load_document_archive()
        self._parse_metadata()
        self._parse_layers()
        self._finalize_animation_frame_count()

    # ── Thumbnail ──────────────────────────────────────────────────────

    def _load_thumbnail(self):
        """Extract the QuickLook thumbnail."""
        candidates = [
            "QuickLook/Thumbnail.png",
            "QuickLook/thumbnail.png",
            "Thumbnail.png",
        ]
        for path in candidates:
            img = self._try_load_image(path)
            if img:
                self.thumbnail = img
                return

        # Fallback: any PNG in QuickLook/
        for name in self._zip.namelist():
            if name.startswith("QuickLook/") and name.lower().endswith(".png"):
                img = self._try_load_image(name)
                if img:
                    self.thumbnail = img
                    return

    def _load_composite(self):
        """Try to load the composite / flattened preview."""
        candidates = [
            "QuickLook/Preview.png",
            "QuickLook/preview.png",
            "composite.png",
        ]
        for path in candidates:
            img = self._try_load_image(path)
            if img:
                self.composite = img
                return

    def _try_load_image(self, zip_path: str) -> Optional[Image.Image]:
        """Attempt to load an image from the ZIP archive."""
        try:
            with self._zip.open(zip_path) as f:
                img = Image.open(io.BytesIO(f.read()))
                img.load()
                return img
        except (KeyError, Exception):
            return None

    # -- Archived Videos -------------------------------------------------

    def _scan_archived_videos(self):
        """Find timelapse/video files stored inside the archive."""
        if not self._zip:
            return

        video_exts = (
            ".mp4", ".mov", ".m4v", ".hevc", ".h264", ".264", ".m3u8", ".ts",
        )
        video_markers = ("video/", "videos/", "timelapse/", "archived")
        self.archived_videos = []

        for info in self._zip.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/")
            lowered = name.lower()
            is_video_path = any(marker in lowered for marker in video_markers)
            is_video_file = lowered.endswith(video_exts)
            if not (is_video_path or is_video_file):
                continue
            if not is_video_file:
                continue
            if lowered.startswith("quicklook/"):
                continue

            self.archived_videos.append(
                {
                    "path": name,
                    "filename": os.path.basename(name),
                    "size": info.file_size,
                }
            )

        if self.archived_videos:
            self.video_enabled = True

    # ── Document Archive ───────────────────────────────────────────────

    def _load_document_archive(self):
        """Parse the Document.archive binary plist."""
        for path in ["Document.archive", "document.archive"]:
            try:
                with self._zip.open(path) as f:
                    self._document_archive = plistlib.loads(f.read())
                    self._archive_objects = self._document_archive.get("$objects", [])
                    return
            except (KeyError, Exception):
                continue

    def _resolve_uid(self, uid) -> Any:
        """Resolve a UID reference in the NSKeyedArchiver plist."""
        if uid is None:
            return None
        idx = None
        if isinstance(uid, int):
            idx = uid
        elif hasattr(uid, "data"):  # plistlib.UID
            idx = int.from_bytes(uid.data, "big") if isinstance(uid.data, bytes) else uid.data
        elif isinstance(uid, plistlib.UID):
            idx = uid
            # In Python 3.8+, plistlib.UID can be used as int
            try:
                idx = int(uid)
            except (TypeError, ValueError):
                return None

        if idx is not None and 0 <= idx < len(self._archive_objects):
            return self._archive_objects[idx]
        return None

    def _get_root_object(self) -> Optional[Dict]:
        """Get the root object from the NSKeyedArchiver."""
        if not self._document_archive:
            return None
        top = self._document_archive.get("$top", {})
        root_uid = top.get("root")
        root = self._resolve_uid(root_uid)
        if isinstance(root, dict):
            return root
        # Fallback: try index 1
        if len(self._archive_objects) > 1 and isinstance(self._archive_objects[1], dict):
            return self._archive_objects[1]
        return None

    # ── Metadata Parsing ───────────────────────────────────────────────

    def _parse_metadata(self):
        """Extract metadata from the root document object."""
        root = self._get_root_object()
        if not root:
            return

        # Canvas dimensions �?try multiple known key names
        self.canvas_width = self._get_int(root, [
            "SilicaDocumentArchiveDimensionWidth",
            "width", "canvasWidth",
        ])
        self.canvas_height = self._get_int(root, [
            "SilicaDocumentArchiveDimensionHeight",
            "height", "canvasHeight",
        ])

        if self.canvas_width == 0 or self.canvas_height == 0:
            size = self._parse_size_value(self._scalar_value(root.get("size")))
            if size:
                self.canvas_width, self.canvas_height = size

        # If dimensions not found, infer from thumbnail
        if self.canvas_width == 0 and self.thumbnail:
            self.canvas_width = self.thumbnail.width
        if self.canvas_height == 0 and self.thumbnail:
            self.canvas_height = self.thumbnail.height

        # DPI
        dpi = self._get_int(root, ["SilicaDocumentArchiveDPI", "dpi"])
        if dpi > 0:
            self.dpi = dpi

        # Orientation
        self.orientation = self._get_int(root, [
            "SilicaDocumentArchiveOrientation", "orientation",
        ])

        # Video recording
        self.video_enabled = self.video_enabled or root.get(
            "SilicaDocumentVideoSegmentInfoKey", False
        ) not in (False, None, "$null")

        self._parse_background_metadata(root)
        self._parse_animation_metadata(root)

        # Color profile
        cp_uid = root.get("SilicaDocumentArchiveICCProfileData")
        cp = self._resolve_uid(cp_uid)
        if isinstance(cp, str):
            self.color_profile = cp

        # Store human-readable metadata
        for k, v in root.items():
            if isinstance(v, (str, int, float, bool)):
                self.metadata[k] = v

    def _parse_background_metadata(self, root: dict):
        """Read Procreate's special Background Color layer."""
        color_value = self._scalar_value(root.get("backgroundColor"))
        color = self._decode_color(color_value)
        if color:
            self.background_color = color

        hidden = self._scalar_value(root.get("backgroundHidden", False))
        self.background_visible = hidden not in (True, "true", "True", 1)
        self.background_layer.visible = self.background_visible

    def _decode_color(self, value) -> Optional[tuple]:
        """Decode archived RGBA float color data to 8-bit RGBA."""
        floats = None
        if isinstance(value, (bytes, bytearray)) and len(value) >= 16:
            for endian in ("<", ">"):
                try:
                    candidate = struct.unpack(endian + "ffff", value[:16])
                except Exception:
                    continue
                if all(0.0 <= component <= 1.0 for component in candidate):
                    floats = candidate
                    break
        elif isinstance(value, (list, tuple)) and len(value) >= 4:
            if all(isinstance(component, (int, float)) for component in value[:4]):
                floats = value[:4]
        if not floats:
            return None
        return tuple(
            max(0, min(255, int(round(float(component) * 255))))
            for component in floats[:4]
        )

    def _parse_animation_metadata(self, root: dict):
        """Extract Animation Assist metadata from known and versioned keys."""
        animation_obj = self._resolved_dict(root.get("animation"))
        if animation_obj:
            self.animation_settings.update(animation_obj)
            self.animation_assist_enabled = True

        enabled_keys = [
            "SilicaDocumentAnimationAssistEnabled",
            "SilicaDocumentArchiveAnimationAssistEnabled",
            "animationAssistEnabled",
            "animationEnabled",
            "isAnimation",
        ]
        for key in enabled_keys:
            val = self._scalar_value(root.get(key))
            if isinstance(val, bool):
                self.animation_assist_enabled = val
                break

        frame_count = self._get_int(root, [
            "SilicaDocumentAnimationFrameCount",
            "SilicaDocumentArchiveAnimationFrameCount",
            "animationFrameCount",
            "frameCount",
        ])
        if frame_count > 0:
            self.animation_frame_count = frame_count

        settings_keys = [
            "SilicaDocumentAnimationSettings",
            "SilicaDocumentArchiveAnimationSettings",
            "animationSettings",
            "AnimationSettings",
        ]
        for key in settings_keys:
            settings = self._resolved_dict(root.get(key))
            if settings:
                self.animation_settings.update(settings)

        for key, val in root.items():
            lowered = str(key).lower()
            if any(token in lowered for token in ("animation", "onion", "fps")):
                scalar = self._scalar_value(val)
                if isinstance(scalar, (str, int, float, bool)):
                    self.animation_settings.setdefault(key, scalar)
                    if isinstance(scalar, bool) and "enabled" in lowered:
                        self.animation_assist_enabled = scalar

        if self.animation_settings and not self.animation_assist_enabled:
            self.animation_assist_enabled = True

        fps = self.animation_settings.get("frameRate")
        if fps is not None:
            self.animation_settings.setdefault("framesPerSecond", fps)

        playback_mode = self.animation_settings.get("playbackMode")
        self.animation_playback_mode = self._decode_playback_mode(playback_mode)
        playback_direction = self.animation_settings.get("playbackDirection")
        self.animation_playback_direction = self._decode_playback_direction(playback_direction)

    def _resolved_dict(self, value) -> Dict[str, Any]:
        """Resolve a UID-backed dictionary to plain scalar values."""
        resolved = self._resolve_uid(value)
        if not isinstance(resolved, dict):
            if isinstance(value, dict):
                resolved = value
            else:
                return {}

        result: Dict[str, Any] = {}
        for key, val in resolved.items():
            scalar = self._scalar_value(val)
            if isinstance(scalar, (str, int, float, bool)):
                result[key] = scalar
        return result

    def _scalar_value(self, value) -> Any:
        """Resolve simple archived scalar values while leaving objects alone."""
        if isinstance(value, (str, int, float, bool)):
            return value
        resolved = self._resolve_uid(value)
        if resolved is not None and not isinstance(resolved, dict):
            return resolved
        return value

    def _decode_playback_mode(self, value) -> str:
        """Map Procreate animation playback mode codes to labels."""
        mapping = {
            0: "loop",
            1: "ping_pong",
            2: "one_shot",
            "loop": "loop",
            "pingPong": "ping_pong",
            "ping_pong": "ping_pong",
            "oneShot": "one_shot",
            "one_shot": "one_shot",
        }
        return mapping.get(value, "loop")

    def _decode_playback_direction(self, value) -> str:
        """Map Procreate animation direction codes to labels."""
        mapping = {
            0: "forward",
            1: "reverse",
            "forward": "forward",
            "reverse": "reverse",
        }
        return mapping.get(value, "forward")

    def _finalize_animation_frame_count(self):
        """Infer frame count from top-level layers when metadata omits it."""
        if self.animation_frame_count > 0 or not self.animation_assist_enabled:
            return
        if self.layer_tree:
            self.animation_frame_count = len(self.get_animation_frames())

    def _get_int(self, d: dict, keys: list, default: int = 0) -> int:
        """Get an integer value trying multiple key names."""
        for key in keys:
            val = d.get(key)
            if val is None:
                continue
            if isinstance(val, (int, float)):
                return int(val)
            if isinstance(val, (bytes, bytearray)):
                try:
                    return struct.unpack(">i", val[:4])[0]
                except Exception:
                    pass
        return default

    def _parse_size_value(self, value) -> Optional[tuple]:
        """Parse archived canvas size values like '{574, 1099}'."""
        if isinstance(value, str):
            match = re.search(r"[-+]?\d+(?:\.\d+)?\s*,\s*[-+]?\d+(?:\.\d+)?", value)
            if match:
                parts = [float(part.strip()) for part in match.group(0).split(",")]
                return int(round(parts[0])), int(round(parts[1]))
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            if all(isinstance(component, (int, float)) for component in value[:2]):
                return int(round(value[0])), int(round(value[1]))
        return None

    def _decode_rect(self, value) -> Optional[tuple]:
        """Decode archived CGRect-like values as ``(x, y, width, height)``."""
        if isinstance(value, (bytes, bytearray)):
            for fmt, size in (
                ("<dddd", 32),
                ("<ffff", 16),
                (">dddd", 32),
                (">ffff", 16),
            ):
                if len(value) < size:
                    continue
                try:
                    rect = struct.unpack(fmt, value[:size])
                except Exception:
                    continue
                return tuple(float(component) for component in rect)
        if isinstance(value, (list, tuple)) and len(value) >= 4:
            if all(isinstance(component, (int, float)) for component in value[:4]):
                return tuple(float(component) for component in value[:4])
        if isinstance(value, str):
            nums = re.findall(r"[-+]?\d+(?:\.\d+)?", value)
            if len(nums) >= 4:
                return tuple(float(num) for num in nums[:4])
        return None

    # ── Layer Parsing ──────────────────────────────────────────────────

    def _parse_layers(self):
        """Parse layer information from the archive objects."""
        root = self._get_root_object()
        if not root:
            return

        # Procreate stores the visible layer tree in unwrappedLayers when
        # top-level folders are present. layers is often a flattened list.
        layers_uid = (
            root.get("unwrappedLayers")
            or root.get("SilicaDocumentArchiveUnwrappedLayers")
            or root.get("SilicaDocumentArchiveLayers")
            or root.get("layers")
        )
        layers_ref = self._resolve_uid(layers_uid)

        # If it's an NSArray wrapper, get the objects list
        if isinstance(layers_ref, dict):
            obj_refs = layers_ref.get("NS.objects", [])
        elif isinstance(layers_ref, list):
            obj_refs = layers_ref
        else:
            obj_refs = []

        self.layer_tree = []
        self.layers = []
        for ref in obj_refs:
            layer = self._parse_layer_ref(ref)
            if layer:
                self.layer_tree.append(layer)
                self._append_layer_flat(layer)

        # Fallback: scan all objects for layer-like dicts
        if not self.layers:
            self._parse_layers_fallback()
            self.layer_tree = list(self.layers)

        self.layer_count = len(self.layers) if self.layers else self.layer_count
        self.folder_count = sum(1 for layer in self.layers if layer.is_folder)

    def _append_layer_flat(self, layer: ProcreateLayer):
        """Append a layer and its children to the flat layer list."""
        self.layers.append(layer)
        for child in layer.children:
            self._append_layer_flat(child)

    def _get_ref_objects(self, value) -> List[Any]:
        """Resolve a UID/list/NSArray-like wrapper to object references."""
        resolved = self._resolve_uid(value)
        if isinstance(resolved, dict):
            refs = resolved.get("NS.objects", resolved.get("objects", []))
        elif isinstance(resolved, list):
            refs = resolved
        elif isinstance(value, list):
            refs = value
        else:
            refs = []
        return refs if isinstance(refs, list) else []

    def _get_classname(self, obj: dict) -> str:
        """Return an archived object's class name when available."""
        cls_ref = obj.get("$class")
        cls_obj = self._resolve_uid(cls_ref)
        if isinstance(cls_obj, dict):
            return str(cls_obj.get("$classname", ""))
        return ""

    def _parse_layer_ref(self, ref) -> Optional[ProcreateLayer]:
        """Parse one layer or group reference, including nested children."""
        layer_dict = self._resolve_uid(ref)
        if not isinstance(layer_dict, dict):
            return None

        name = self._resolve_layer_field(layer_dict, [
            "name", "SilicaLayerArchiveName", "groupName", "folderName",
        ])
        if not isinstance(name, str):
            name = f"Layer {len(self.layers) + 1}"

        uuid = self._resolve_layer_field(layer_dict, [
            "UUID", "uuid", "SilicaLayerArchiveUUID",
        ])
        if not isinstance(uuid, str):
            uuid = ""

        opacity = layer_dict.get(
            "contentsOpacity",
            layer_dict.get("opacity", layer_dict.get("SilicaLayerArchiveOpacity", 1.0)),
        )
        if not isinstance(opacity, (int, float)):
            opacity = 1.0

        hidden = self._scalar_value(
            layer_dict.get(
                "hidden",
                layer_dict.get(
                    "isHidden",
                    layer_dict.get("SilicaLayerArchiveHidden", False),
                ),
            )
        )
        visible = hidden not in (True, "true", "True", 1)

        blend = layer_dict.get(
            "extendedBlend",
            layer_dict.get("blend", layer_dict.get("blendMode", 0)),
        )
        if not isinstance(blend, (int, float)):
            blend = 0

        animation_held_length = self._scalar_value(
            layer_dict.get("animationHeldLength", 0)
        )
        if not isinstance(animation_held_length, (int, float)):
            animation_held_length = 0

        contents_rect = None
        contents_rect_valid = self._scalar_value(
            layer_dict.get("contentsRectValid", False)
        )
        if contents_rect_valid in (True, "true", "True", 1):
            contents_rect = self._decode_rect(
                self._scalar_value(layer_dict.get("contentsRect"))
            )

        child_refs: List[Any] = []
        for key in (
            "children",
            "layers",
            "sublayers",
            "NS.objects",
            "SilicaGroupLayerArchiveChildren",
            "SilicaLayerGroupArchiveChildren",
        ):
            child_refs = self._get_ref_objects(layer_dict.get(key))
            if child_refs:
                break

        classname = self._get_classname(layer_dict)
        is_folder = bool(child_refs) or any(
            marker in classname.lower() for marker in ("group", "folder")
        ) or bool(layer_dict.get("isGroup", layer_dict.get("isFolder", False)))

        children = []
        for child_ref in child_refs:
            child = self._parse_layer_ref(child_ref)
            if child:
                children.append(child)

        return ProcreateLayer(
            name=name,
            uuid=uuid,
            opacity=float(opacity),
            visible=visible,
            blend_mode=int(blend),
            is_folder=is_folder,
            children=children,
            animation_held_length=int(animation_held_length),
            contents_rect=contents_rect,
        )

    def _resolve_layer_field(self, layer_dict: dict, keys: list) -> Any:
        """Resolve a layer field that might be a UID reference."""
        for key in keys:
            val = layer_dict.get(key)
            if val is None:
                continue
            resolved = self._resolve_uid(val)
            if resolved is not None and not isinstance(resolved, dict):
                return resolved
            if isinstance(val, str):
                return val
        return None

    def _parse_layers_fallback(self):
        """Fallback: scan archive objects for anything that looks like a layer."""
        for obj in self._archive_objects:
            if not isinstance(obj, dict):
                continue
            # Check for Silica layer class markers
            cls_ref = obj.get("$class")
            if cls_ref is None:
                continue
            cls_obj = self._resolve_uid(cls_ref)
            if not isinstance(cls_obj, dict):
                continue
            classname = cls_obj.get("$classname", "")
            if "SilicaLayer" not in classname:
                continue

            name_ref = obj.get("name", obj.get("SilicaLayerArchiveName"))
            name = self._resolve_uid(name_ref)
            if not isinstance(name, str):
                name = f"Layer {len(self.layers) + 1}"

            opacity = obj.get("contentsOpacity", obj.get("opacity", 1.0))
            if not isinstance(opacity, (int, float)):
                opacity = 1.0

            visible = not obj.get("hidden", False)

            layer = ProcreateLayer(
                name=name,
                uuid="",
                opacity=float(opacity),
                visible=bool(visible),
                blend_mode=0,
            )
            self.layers.append(layer)

    # ── Layer Image Loading & Compositing ──────────────────────────────

    def _get_tile_size(self) -> int:
        """Get the tile size used for chunk storage."""
        root = self._get_root_object()
        if root:
            ts = self._get_int(root, [
                "tileSize",
                "SilicaDocumentArchiveTileSize",
            ])
            if ts > 0:
                return ts
        return 256

    def load_layer_image(self, layer_index: int) -> Optional[Image.Image]:
        """Load pixel data for a specific layer from chunk files.

        Returns an RGBA Image sized to the canvas, or None if the
        layer's raw data cannot be decoded.
        """
        if not self._zip or layer_index < 0 or layer_index >= len(self.layers):
            return None

        layer = self.layers[layer_index]
        uuid = layer.uuid
        if not uuid:
            return None

        all_names = self._zip.namelist()
        prefix = f"{uuid}/"
        # Accept both .chunk and .lz4 tile files
        _tile_exts = (".chunk", ".lz4")
        chunk_files = [
            n for n in all_names
            if n.startswith(prefix) and n.lower().endswith(_tile_exts)
        ]
        if not chunk_files:
            return None

        tile_size = self._get_tile_size()
        if tile_size <= 0:
            tile_size = 256

        w, h = self.canvas_width, self.canvas_height
        if w <= 0 or h <= 0:
            return None

        cols = max(1, (w + tile_size - 1) // tile_size)
        rows = max(1, (h + tile_size - 1) // tile_size)

        layer_img = Image.new(
            "RGBA", (cols * tile_size, rows * tile_size), (0, 0, 0, 0)
        )
        loaded_any = False

        for chunk_path in chunk_files:
            basename = chunk_path[len(prefix):]
            # Strip any known tile extension
            name_part = basename
            for _ext in (".chunk", ".lz4"):
                if name_part.lower().endswith(_ext):
                    name_part = name_part[:-len(_ext)]
                    break
            # Support both '_' and '~' as col/row separator
            for sep in ("~", "_"):
                if sep in name_part:
                    parts = name_part.split(sep)
                    break
            else:
                continue
            if len(parts) != 2:
                continue
            try:
                col = int(parts[0])
                row = int(parts[1])
            except ValueError:
                continue
            tile_width = min(tile_size, w - col * tile_size)
            tile_height = min(tile_size, h - row * tile_size)
            if tile_width <= 0 or tile_height <= 0:
                continue
            full_tile_size = tile_size * tile_size * 4
            stored_tile_size = tile_width * tile_height * 4

            try:
                raw = self._zip.read(chunk_path)
            except Exception:
                continue

            pixels = None

            # Method 0: bv41 (Apple/Procreate custom lz4 with chained blocks)
            if raw[:4] == b"bv41":
                try:
                    import lz4.block as _lz4b
                    off = 0
                    bv_parts: list = []
                    prev_dict = b""
                    while off < len(raw):
                        if raw[off:off + 4] == b"bv4$":
                            break
                        if raw[off:off + 4] != b"bv41":
                            break
                        off += 4
                        u_size = struct.unpack_from("<I", raw, off)[0]
                        off += 4
                        c_size = struct.unpack_from("<I", raw, off)[0]
                        off += 4
                        chunk = _lz4b.decompress(
                            raw[off:off + c_size],
                            uncompressed_size=u_size,
                            dict=prev_dict,
                        )
                        bv_parts.append(chunk)
                        prev_dict = chunk
                        off += c_size
                    pixels = b"".join(bv_parts)
                except Exception:
                    pixels = None

            # Method 1: uncompressed RGBA
            if pixels is None and len(raw) in (stored_tile_size, full_tile_size):
                pixels = raw

            # Method 2: lz4 block
            if pixels is None:
                for size in dict.fromkeys((stored_tile_size, full_tile_size)):
                    try:
                        import lz4.block
                        pixels = lz4.block.decompress(
                            raw, uncompressed_size=size
                        )
                        break
                    except Exception:
                        pixels = None

            # Method 3: lzo
            if pixels is None:
                for size in dict.fromkeys((stored_tile_size, full_tile_size)):
                    try:
                        import lzo
                        pixels = lzo.decompress(raw, False, size)
                        break
                    except Exception:
                        pixels = None

            # Method 4: zlib
            if pixels is None:
                try:
                    import zlib
                    pixels = zlib.decompress(raw)
                except Exception:
                    pass

            if pixels is None or len(pixels) not in (stored_tile_size, full_tile_size):
                continue
            pixel_size = (
                (tile_width, tile_height)
                if len(pixels) == stored_tile_size
                else (tile_size, tile_size)
            )

            tile_img = Image.frombytes(
                "RGBA", pixel_size, pixels, "raw", "RGBA"
            )
            tile_img = self._unpremultiply_alpha(tile_img)
            layer_img.paste(tile_img, (col * tile_size, row * tile_size))
            loaded_any = True

        if not loaded_any:
            return None

        if layer_img.size != (w, h):
            layer_img = layer_img.crop((0, 0, w, h))
        layer_img = layer_img.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        return layer_img

    def _unpremultiply_alpha(self, image: Image.Image) -> Image.Image:
        """Convert Procreate premultiplied RGBA tile data to straight alpha."""
        alpha = image.getchannel("A")
        if alpha.getextrema() in ((0, 0), (255, 255)):
            return image

        raw = bytearray(image.tobytes())
        changed = False
        for offset in range(0, len(raw), 4):
            a = raw[offset + 3]
            if a in (0, 255):
                continue
            r = min(255, round(raw[offset] * 255 / a))
            g = min(255, round(raw[offset + 1] * 255 / a))
            b = min(255, round(raw[offset + 2] * 255 / a))
            if (r, g, b) != (raw[offset], raw[offset + 1], raw[offset + 2]):
                raw[offset] = r
                raw[offset + 1] = g
                raw[offset + 2] = b
                changed = True

        if not changed:
            return image
        return Image.frombytes("RGBA", image.size, bytes(raw))

    def composite_layers(
        self,
        visibility_overrides: Optional[Dict[int, bool]] = None,
    ) -> Optional[Image.Image]:
        """Composite layers respecting custom visibility overrides.

        Args:
            visibility_overrides: ``{layer_index: visible}`` dict that
                overrides every layer's native *visible* flag.

        Returns:
            An RGBA ``Image``, or ``None`` when no layer data could be
            loaded (caller should fall back to ``get_best_image()``).
        """
        if not self.layers:
            return None

        w, h = self.canvas_width, self.canvas_height
        if w <= 0 or h <= 0:
            return None

        result = self._blank_canvas(transparent_background=False)
        loaded_any = False

        for layer in self._iter_visible_composite_layers(visibility_overrides):
            try:
                i = self.layers.index(layer)
            except ValueError:
                continue
            layer_img = self.load_layer_image(i)
            if layer_img is None:
                continue
            loaded_any = True

            if layer.opacity < 1.0:
                r, g, b, a = layer_img.split()
                a = a.point(lambda x, op=layer.opacity: int(x * op))
                layer_img = Image.merge("RGBA", (r, g, b, a))

            result = Image.alpha_composite(result, layer_img)

        return result if loaded_any else None

    def _iter_visible_composite_layers(
        self,
        visibility_overrides: Optional[Dict[int, bool]] = None,
    ) -> List[ProcreateLayer]:
        """Return visible leaves in bottom-to-top compositing order."""
        roots = self.layer_tree or self.layers
        if self.animation_assist_enabled and self.layer_tree:
            roots = self._current_animation_preview_roots(visibility_overrides)
        else:
            roots = list(reversed(roots))

        result: List[ProcreateLayer] = []

        def visit(layer: ProcreateLayer, parent_visible: bool = True):
            try:
                idx = self.layers.index(layer)
            except ValueError:
                idx = -1
            own_visible = (
                self._layer_visible_with_overrides(idx, visibility_overrides)
                if idx >= 0
                else layer.visible
            )
            visible = parent_visible and own_visible
            if not visible:
                return
            if layer.is_folder:
                for child in reversed(layer.children):
                    visit(child, visible)
                return
            result.append(layer)

        for root in roots:
            visit(root)
        return result

    def _current_animation_preview_roots(
        self,
        visibility_overrides: Optional[Dict[int, bool]] = None,
    ) -> List[ProcreateLayer]:
        """Pick the visible Animation Assist frame to show in static preview."""
        for layer in self.layer_tree:
            try:
                idx = self.layers.index(layer)
            except ValueError:
                idx = -1
            visible = (
                self._layer_visible_with_overrides(idx, visibility_overrides)
                if idx >= 0
                else layer.visible
            )
            if visible:
                return [layer]
        return []

    def _layer_visible_with_overrides(
        self,
        index: int,
        visibility_overrides: Optional[Dict[int, bool]] = None,
    ) -> bool:
        """Return layer visibility after explicit preview overrides."""
        if visibility_overrides and index in visibility_overrides:
            return visibility_overrides[index]
        return self.layers[index].visible

    def _blank_canvas(self, transparent_background: bool = False) -> Image.Image:
        """Create a canvas with the Procreate background color if enabled."""
        w, h = self.canvas_width, self.canvas_height
        if w <= 0 or h <= 0:
            raise ValueError("Canvas size is unknown")
        if transparent_background or not self.background_visible:
            return Image.new("RGBA", (w, h), (0, 0, 0, 0))
        return Image.new("RGBA", (w, h), self.background_color)

    def render_layer_item(
        self,
        layer: ProcreateLayer,
        transparent_background: bool = False,
    ) -> Optional[Image.Image]:
        """Render a layer or folder as a standalone animation frame."""
        result = self._blank_canvas(transparent_background)
        loaded_any = False

        for child in self._iter_renderable_layers(layer):
            if not child.visible:
                continue
            try:
                idx = self.layers.index(child)
            except ValueError:
                continue
            layer_img = self.load_layer_image(idx)
            if layer_img is None:
                continue
            loaded_any = True
            if child.opacity < 1.0:
                r, g, b, a = layer_img.split()
                a = a.point(lambda x, op=child.opacity: int(x * op))
                layer_img = Image.merge("RGBA", (r, g, b, a))
            result = Image.alpha_composite(result, layer_img)

        if loaded_any:
            return result
        return result if layer.is_background_color else None

    def _iter_renderable_layers(self, layer: ProcreateLayer) -> List[ProcreateLayer]:
        """Return leaf layers in bottom-to-top render order."""
        if not layer.is_folder:
            return [layer]
        result: List[ProcreateLayer] = []
        for child in reversed(layer.children):
            result.extend(self._iter_renderable_layers(child))
        return result

    def render_animation_frames(
        self,
        fps: Optional[int] = None,
        transparent_background: bool = False,
        include_playback_sequence: bool = False,
        expand_holds: bool = True,
    ) -> List[Image.Image]:
        """Render animation frames using top-level Animation Assist items."""
        frame_items = (
            self.get_animation_playback_sequence()
            if include_playback_sequence
            else self.get_animation_frames(expand_holds=expand_holds)
        )
        if not frame_items:
            raise ValueError("No visible animation frames found")
        images = [
            self.render_layer_item(frame, transparent_background=transparent_background)
            for frame in frame_items
        ]
        if any(image is None for image in images):
            raise ValueError("Animation frames could not be rendered from layer data")
        return [image for image in images if image is not None]

    # ── Public Helpers ─────────────────────────────────────────────────

    def get_best_image(self) -> Optional[Image.Image]:
        """Return the best available image (composite > thumbnail)."""
        return self.composite or self.thumbnail

    def get_blend_mode_name(self, mode: int) -> str:
        """Get human-readable blend mode name."""
        return self.BLEND_MODES.get(mode, f"Unknown ({mode})")

    def get_file_list(self) -> List[str]:
        """List all entries in the ZIP archive."""
        return self._zip.namelist() if self._zip else []

    def get_animation_frames(self, expand_holds: bool = True) -> List[ProcreateLayer]:
        """Return animation frames from visible top-level layers/folders.

        Procreate Animation Assist treats each visible top-level layer or
        group as a frame. animationHeldLength adds duplicate held frames
        after the default frame.
        """
        frames: List[ProcreateLayer] = []
        for layer in self.layer_tree:
            if not layer.visible:
                continue
            repeat = 1 + layer.animation_held_length if expand_holds else 1
            frames.extend([layer] * max(1, repeat))
        return frames

    def get_animation_playback_sequence(self) -> List[ProcreateLayer]:
        """Return one playback cycle respecting loop/ping-pong/one-shot mode."""
        frames = self.get_animation_frames(expand_holds=True)
        if self.animation_playback_direction == "reverse":
            frames = list(reversed(frames))
        if self.animation_playback_mode == "ping_pong" and len(frames) > 1:
            frames = frames + frames[-2:0:-1]
        return frames

    def list_archived_videos(self) -> List[Dict[str, Any]]:
        """List timelapse/video entries stored inside the archive."""
        return list(self.archived_videos)

    def list_archived_video_segments(self) -> List[Dict[str, Any]]:
        """List archived video segments in numeric segment order."""
        segments = [
            entry for entry in self.archived_videos
            if re.search(r"segment-(\d+)\.[^.\\/]+$", entry["path"], re.IGNORECASE)
        ]

        def segment_index(entry: Dict[str, Any]) -> int:
            match = re.search(r"segment-(\d+)\.[^.\\/]+$", entry["path"], re.IGNORECASE)
            return int(match.group(1)) if match else 0

        return sorted(segments, key=segment_index)

    def get_file_size(self) -> int:
        """Get the .procreate file size in bytes."""
        return os.path.getsize(self.filepath)

    def get_file_size_human(self) -> str:
        """Get human-readable file size."""
        size = self.get_file_size()
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"

    def export_image(self, output_path: str, fmt: str = "PNG", quality: int = 95):
        """Export the best available image to a standard format."""
        img = self.get_best_image()
        if not img:
            raise ValueError("No image data available to export")
        save_kwargs = {"format": fmt}
        if fmt.upper() in ("JPEG", "JPG"):
            save_kwargs["quality"] = quality
            if img.mode == "RGBA":
                # Flatten alpha for JPEG
                bg = Image.new("RGB", img.size, (255, 255, 255))
                bg.paste(img, mask=img.split()[3])
                img = bg
        img.save(output_path, **save_kwargs)

    def export_animation_gif(
        self,
        output_path: str,
        fps: Optional[int] = None,
        maximum_resolution: bool = True,
        use_grid: bool = False,
        dither: bool = True,
        per_frame_palette: bool = True,
        transparent_background: bool = False,
        alpha_threshold: int = 0,
    ) -> str:
        """Export Animation Assist frames as an animated GIF."""
        frames = self.render_animation_frames(
            fps=fps,
            transparent_background=transparent_background,
            expand_holds=True,
        )
        frames = [
            self._prepare_gif_frame(
                frame,
                dither=dither,
                per_frame_palette=per_frame_palette,
                transparent_background=transparent_background,
                alpha_threshold=alpha_threshold,
            )
            for frame in frames
        ]
        duration = self._frame_duration_ms(fps)
        save_kwargs = {
            "save_all": True,
            "append_images": frames[1:],
            "duration": duration,
            "loop": 0 if self.animation_playback_mode != "one_shot" else 1,
            "disposal": 2,
        }
        if transparent_background:
            save_kwargs["transparency"] = 0
        frames[0].save(output_path, format="GIF", **save_kwargs)
        return output_path

    def export_animation_apng(
        self,
        output_path: str,
        fps: Optional[int] = None,
        maximum_resolution: bool = True,
        use_grid: bool = False,
        transparent_background: bool = False,
    ) -> str:
        """Export Animation Assist frames as animated PNG."""
        frames = self.render_animation_frames(
            fps=fps,
            transparent_background=transparent_background,
            expand_holds=True,
        )
        duration = self._frame_duration_ms(fps)
        frames[0].save(
            output_path,
            format="PNG",
            save_all=True,
            append_images=frames[1:],
            duration=duration,
            loop=0 if self.animation_playback_mode != "one_shot" else 1,
        )
        return output_path

    def export_animation_png_sequence(
        self,
        output_folder: str,
        fps: Optional[int] = None,
        maximum_resolution: bool = True,
        use_grid: bool = False,
        transparent_background: bool = False,
        expand_holds: bool = True,
    ) -> str:
        """Export every animation frame as PNG plus JSON metadata."""
        frames = self.render_animation_frames(
            fps=fps,
            transparent_background=transparent_background,
            expand_holds=expand_holds,
        )
        frame_items = self.get_animation_frames(expand_holds=expand_holds)
        os.makedirs(output_folder, exist_ok=True)
        frame_records = []
        for i, frame in enumerate(frames, start=1):
            filename = f"frame_{i:04d}.png"
            frame.save(os.path.join(output_folder, filename), format="PNG")
            source_layer = frame_items[i - 1] if i - 1 < len(frame_items) else None
            source = source_layer.name if source_layer else ""
            hold_frames = source_layer.animation_held_length if source_layer else 0
            frame_records.append({
                "file": filename,
                "source": source,
                "hold_frames": hold_frames,
                "duration_frames": 1 + hold_frames,
            })

        metadata = {
            "fps": self._effective_fps(fps),
            "frame_count": len(frames),
            "expanded_frame_count": len(self.get_animation_frames(expand_holds=True)),
            "exported_with_repeated_holds": expand_holds,
            "playback_mode": self.animation_playback_mode,
            "playback_direction": self.animation_playback_direction,
            "transparent_background": transparent_background,
            "canvas": {
                "width": self.canvas_width,
                "height": self.canvas_height,
            },
            "frames": frame_records,
        }
        metadata_path = os.path.join(output_folder, "animation.json")
        with open(metadata_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2)
        return metadata_path

    def export_animation_video(
        self,
        output_path: str,
        fmt: str = "mp4",
        fps: Optional[int] = None,
        maximum_resolution: bool = True,
        use_grid: bool = False,
        transparent_background: bool = False,
        ffmpeg_path: str = "ffmpeg",
    ) -> str:
        """Export Animation Assist frames as MP4 or HEVC using ffmpeg."""
        fmt = fmt.lower()
        if fmt not in ("mp4", "hevc", "hebc"):
            raise ValueError("fmt must be 'mp4' or 'hevc'")
        fps_value = self._effective_fps(fps)
        with tempfile.TemporaryDirectory(prefix="procreate_anim_") as tmp:
            self.export_animation_png_sequence(
                tmp,
                fps=fps_value,
                transparent_background=transparent_background,
            )
            input_pattern = os.path.join(tmp, "frame_%04d.png")
            codec_args = (
                ["-c:v", "libx265", "-tag:v", "hvc1"]
                if fmt in ("hevc", "hebc")
                else ["-c:v", "libx264"]
            )
            pix_fmt = "yuva420p" if fmt in ("hevc", "hebc") and transparent_background else "yuv420p"
            command = [
                ffmpeg_path, "-y", "-framerate", str(fps_value),
                "-i", input_pattern,
                *codec_args,
                "-pix_fmt", pix_fmt,
                "-movflags", "+faststart",
                output_path,
            ]
            try:
                subprocess.run(command, check=True, capture_output=True, text=True)
            except subprocess.CalledProcessError as exc:
                detail = (exc.stderr or exc.stdout or "").strip()
                if fmt in ("hevc", "hebc") and transparent_background:
                    raise ValueError(
                        "Transparent HEVC export is not supported by this ffmpeg "
                        "encoder. Try HEVC without transparency, APNG, or PNG frames."
                    ) from exc
                raise ValueError(f"ffmpeg animation export failed: {detail}") from exc
        return output_path

    def _effective_fps(self, fps: Optional[int] = None) -> int:
        """Return export FPS, falling back to animation metadata."""
        value = fps or self.animation_settings.get("framesPerSecond") or 12
        return max(1, int(value))

    def _frame_duration_ms(self, fps: Optional[int] = None) -> int:
        return max(1, int(round(1000 / self._effective_fps(fps))))

    def _prepare_gif_frame(
        self,
        frame: Image.Image,
        dither: bool,
        per_frame_palette: bool,
        transparent_background: bool,
        alpha_threshold: int,
    ) -> Image.Image:
        """Prepare an RGBA frame for GIF export options."""
        if transparent_background:
            alpha_threshold = max(0, min(100, alpha_threshold))
            threshold = int(255 * (alpha_threshold / 100.0))
            frame = frame.copy()
            alpha = frame.getchannel("A").point(lambda p: 0 if p <= threshold else 255)
            frame.putalpha(alpha)
        dither_mode = Image.FLOYDSTEINBERG if dither else Image.Dither.NONE
        palette = Image.Palette.ADAPTIVE if per_frame_palette else Image.ADAPTIVE
        return frame.convert("P", palette=palette, dither=dither_mode)

    def export_archived_videos(self, output_folder: str) -> List[str]:
        """Export archived timelapse/video entries to a folder.

        Returns the file paths written on disk. The archive's relative
        video path is preserved below ``output_folder``.
        """
        if not self._zip:
            raise ValueError("Archive is closed")
        if not self.archived_videos:
            return []

        os.makedirs(output_folder, exist_ok=True)
        exported: List[str] = []
        base = os.path.abspath(output_folder)

        for entry in self.archived_videos:
            rel = entry["path"].replace("\\", "/").lstrip("/")
            rel_parts = [
                part for part in rel.split("/")
                if part and part not in (".", "..")
            ]
            if not rel_parts:
                continue
            dest = os.path.abspath(os.path.join(base, *rel_parts))
            if os.path.commonpath([base, dest]) != base:
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with self._zip.open(entry["path"]) as src, open(dest, "wb") as out:
                out.write(src.read())
            exported.append(dest)

        return exported

    def merge_archived_video_segments(
        self,
        output_path: str,
        duration_mode: str = "full",
        ffmpeg_path: str = "ffmpeg",
        ffprobe_path: str = "ffprobe",
    ) -> str:
        """Merge archived MP4 segments into one video.

        Args:
            output_path: Destination video path.
            duration_mode: ``"full"`` for the original full length, or
                ``"30s"`` to speed the full timelapse into 30 seconds.
            ffmpeg_path: ffmpeg executable.
            ffprobe_path: ffprobe executable used for 30-second export.

        Returns:
            The output path.
        """
        if not self._zip:
            raise ValueError("Archive is closed")

        segments = self.list_archived_video_segments()
        if not segments:
            raise ValueError("No archived video segments found")

        duration_mode = duration_mode.lower()
        if duration_mode not in ("full", "30s", "30sec", "30_seconds"):
            raise ValueError("duration_mode must be 'full' or '30s'")

        output_path = os.path.abspath(output_path)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        with tempfile.TemporaryDirectory(prefix="procreate_video_") as tmp:
            concat_path = os.path.join(tmp, "segments.txt")
            extracted_paths: List[str] = []
            for i, entry in enumerate(segments):
                ext = os.path.splitext(entry["filename"])[1] or ".mp4"
                dest = os.path.join(tmp, f"segment-{i:05d}{ext}")
                with self._zip.open(entry["path"]) as src, open(dest, "wb") as out:
                    out.write(src.read())
                extracted_paths.append(dest)

            with open(concat_path, "w", encoding="utf-8") as f:
                for path in extracted_paths:
                    safe_path = path.replace("\\", "/").replace("'", "'\\''")
                    f.write(f"file '{safe_path}'\n")

            if duration_mode == "full":
                command = [
                    ffmpeg_path, "-y", "-f", "concat", "-safe", "0",
                    "-i", concat_path, "-c", "copy", output_path,
                ]
                subprocess.run(command, check=True, capture_output=True, text=True)
                return output_path

            merged_path = os.path.join(tmp, "merged-full.mp4")
            subprocess.run(
                [
                    ffmpeg_path, "-y", "-f", "concat", "-safe", "0",
                    "-i", concat_path, "-c", "copy", merged_path,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            duration = self._probe_video_duration(merged_path, ffprobe_path)
            if duration <= 0:
                raise ValueError("Could not determine merged video duration")
            setpts = max(0.001, 30.0 / duration)
            subprocess.run(
                [
                    ffmpeg_path, "-y", "-i", merged_path,
                    "-filter:v", f"setpts={setpts:.8f}*PTS",
                    "-an", "-movflags", "+faststart", output_path,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            return output_path

    def _probe_video_duration(self, path: str, ffprobe_path: str = "ffprobe") -> float:
        """Return video duration in seconds using ffprobe."""
        result = subprocess.run(
            [
                ffprobe_path, "-v", "error", "-show_entries",
                "format=duration", "-of",
                "default=noprint_wrappers=1:nokey=1", path,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return float(result.stdout.strip())

    def extract_thumbnail_bytes(self) -> Optional[bytes]:
        """Get raw PNG bytes of the thumbnail (for shell extension use)."""
        if not self._zip:
            return None
        for path in ["QuickLook/Thumbnail.png", "QuickLook/thumbnail.png", "Thumbnail.png"]:
            try:
                return self._zip.read(path)
            except KeyError:
                continue
        return None

    # ── Context Manager ────────────────────────────────────────────────

    def close(self):
        if self._zip:
            self._zip.close()
            self._zip = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def __del__(self):
        self.close()

    def __repr__(self):
        return (
            f"ProcreateFile('{self.filename}', "
            f"{self.canvas_width}x{self.canvas_height}, "
            f"{self.layer_count} layers)"
        )
