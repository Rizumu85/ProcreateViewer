import io
import os
import plistlib
import tempfile
import unittest
import zipfile
import shutil
import json

from PIL import Image

import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from procreate_reader import ProcreateFile  # noqa: E402


def _png_bytes():
    buf = io.BytesIO()
    Image.new("RGBA", (2, 2), (255, 0, 0, 255)).save(buf, format="PNG")
    return buf.getvalue()


def _rgba_bytes(color, size=2):
    r, g, b, a = color
    return bytes([r, g, b, a]) * (size * size)


def _rgba_pixels(colors):
    data = bytearray()
    for r, g, b, a in colors:
        data.extend([r, g, b, a])
    return bytes(data)


def _rgba_grid(rows):
    data = bytearray()
    for row in rows:
        for r, g, b, a in row:
            data.extend([r, g, b, a])
    return bytes(data)


def _archive(objects, root_index=1):
    return plistlib.dumps(
        {
            "$version": 100000,
            "$archiver": "NSKeyedArchiver",
            "$top": {"root": plistlib.UID(root_index)},
            "$objects": objects,
        },
        fmt=plistlib.FMT_BINARY,
    )


def _write_procreate(entries, archive_objects):
    tmp = tempfile.NamedTemporaryFile(suffix=".procreate", delete=False)
    tmp.close()
    with zipfile.ZipFile(tmp.name, "w") as zf:
        zf.writestr("QuickLook/Thumbnail.png", _png_bytes())
        zf.writestr("Document.archive", _archive(archive_objects))
        for name, data in entries.items():
            zf.writestr(name, data)
    return tmp.name


class ProcreateReaderTests(unittest.TestCase):
    def tearDown(self):
        path = getattr(self, "_path", None)
        if path and os.path.exists(path):
            os.remove(path)
        out_dir = getattr(self, "_out_dir", None)
        if out_dir and os.path.isdir(out_dir):
            shutil.rmtree(out_dir)

    def test_reads_layer_folders_as_a_hierarchy(self):
        objects = [
            "$null",
            {"width": 2, "height": 2, "layers": plistlib.UID(2)},
            {"NS.objects": [plistlib.UID(3), plistlib.UID(7)]},
            {
                "$class": plistlib.UID(9),
                "name": plistlib.UID(4),
                "children": plistlib.UID(5),
            },
            "Characters",
            {"NS.objects": [plistlib.UID(6)]},
            {"name": "Line Art", "uuid": "line-art"},
            {"name": "Background", "uuid": "background"},
            "unused",
            {"$classname": "SilicaGroupLayer"},
        ]
        self._path = _write_procreate({}, objects)

        with ProcreateFile(self._path) as pf:
            self.assertEqual(len(pf.layer_tree), 2)
            folder = pf.layer_tree[0]
            self.assertTrue(folder.is_folder)
            self.assertEqual(folder.name, "Characters")
            self.assertEqual([child.name for child in folder.children], ["Line Art"])
            self.assertEqual([layer.name for layer in pf.layers], ["Characters", "Line Art", "Background"])

    def test_reads_animation_assist_frame_count_and_settings(self):
        objects = [
            "$null",
            {
                "width": 2,
                "height": 2,
                "unwrappedLayers": plistlib.UID(2),
                "animation": plistlib.UID(5),
            },
            {"NS.objects": [plistlib.UID(3), plistlib.UID(4)]},
            {"name": "Frame 1", "uuid": "frame-1", "animationHeldLength": 4},
            {"name": "Frame 2", "uuid": "frame-2", "hidden": True},
            {
                "$class": plistlib.UID(6),
                "frameRate": 24,
                "playbackMode": 1,
                "playbackDirection": 0,
                "onionSkinFrameCount": 3,
            },
            {"$classname": "ValkyrieDocumentAnimation"},
        ]
        self._path = _write_procreate({}, objects)

        with ProcreateFile(self._path) as pf:
            self.assertTrue(pf.animation_assist_enabled)
            self.assertEqual(pf.animation_frame_count, 5)
            self.assertEqual(pf.animation_settings["framesPerSecond"], 24)
            self.assertEqual(pf.animation_playback_mode, "ping_pong")
            self.assertEqual(pf.animation_settings["onionSkinFrameCount"], 3)
            self.assertEqual([frame.name for frame in pf.get_animation_frames()], ["Frame 1"] * 5)

    def test_lists_and_exports_archived_video_entries(self):
        objects = [
            "$null",
            {"width": 2, "height": 2, "layers": plistlib.UID(2)},
            {"NS.objects": []},
        ]
        self._path = _write_procreate(
            {
                "video/segments/segment-0001.mp4": b"video-one",
                "video/segments/segment-0010.mp4": b"video-ten",
                "video/segments/segment-0002.mp4": b"video-two",
                "QuickLook/Preview.png": _png_bytes(),
                "Document.archive.tmp": b"not video",
            },
            objects,
        )
        self._out_dir = tempfile.mkdtemp()

        with ProcreateFile(self._path) as pf:
            videos = pf.list_archived_videos()
            self.assertEqual(len(videos), 3)
            self.assertEqual(videos[0]["path"], "video/segments/segment-0001.mp4")
            self.assertTrue(pf.video_enabled)
            self.assertEqual(
                [entry["path"] for entry in pf.list_archived_video_segments()],
                [
                    "video/segments/segment-0001.mp4",
                    "video/segments/segment-0002.mp4",
                    "video/segments/segment-0010.mp4",
                ],
            )

            exported = pf.export_archived_videos(self._out_dir)

        self.assertEqual(len(exported), 3)
        self.assertTrue(os.path.exists(exported[0]))
        with open(exported[0], "rb") as f:
            self.assertEqual(f.read(), b"video-one")

    def test_reads_background_color_as_a_special_layer(self):
        objects = [
            "$null",
            {
                "width": 2,
                "height": 2,
                "layers": plistlib.UID(2),
                "backgroundColor": plistlib.UID(3),
                "backgroundHidden": False,
            },
            {"NS.objects": []},
            struct_pack_floats(0.25, 0.5, 0.75, 1.0),
        ]
        self._path = _write_procreate({}, objects)

        with ProcreateFile(self._path) as pf:
            self.assertEqual(pf.background_color, (64, 128, 191, 255))
            self.assertTrue(pf.background_visible)
            self.assertTrue(pf.background_layer.is_background_color)
            self.assertEqual(pf.background_layer.name, "Background Color")

    def test_exports_animation_gif_apng_and_png_sequence(self):
        objects = [
            "$null",
            {
                "width": 2,
                "height": 2,
                "tileSize": 2,
                "unwrappedLayers": plistlib.UID(2),
                "animation": plistlib.UID(6),
                "backgroundColor": plistlib.UID(7),
            },
            {"NS.objects": [plistlib.UID(3), plistlib.UID(4)]},
            {"name": "Red", "uuid": "red-frame"},
            {"name": "Blue", "uuid": "blue-frame", "animationHeldLength": 1},
            "unused",
            {"frameRate": 12, "playbackMode": 2},
            struct_pack_floats(1.0, 1.0, 1.0, 1.0),
        ]
        self._path = _write_procreate(
            {
                "red-frame/0_0.chunk": _rgba_bytes((255, 0, 0, 255)),
                "blue-frame/0_0.chunk": _rgba_bytes((0, 0, 255, 255)),
            },
            objects,
        )
        self._out_dir = tempfile.mkdtemp()

        with ProcreateFile(self._path) as pf:
            gif_path = os.path.join(self._out_dir, "anim.gif")
            apng_path = os.path.join(self._out_dir, "anim.png")
            png_dir = os.path.join(self._out_dir, "frames")

            pf.export_animation_gif(gif_path, fps=12, dither=False)
            pf.export_animation_apng(apng_path, fps=12)
            metadata_path = pf.export_animation_png_sequence(png_dir, fps=12)

        self.assertTrue(os.path.exists(gif_path))
        self.assertTrue(os.path.exists(apng_path))
        self.assertTrue(os.path.exists(os.path.join(png_dir, "frame_0001.png")))
        self.assertTrue(os.path.exists(os.path.join(png_dir, "frame_0003.png")))
        with open(metadata_path, "r", encoding="utf-8") as f:
            metadata = json.load(f)
        self.assertEqual(metadata["fps"], 12)
        self.assertEqual(metadata["frame_count"], 3)
        self.assertEqual(metadata["frames"][1]["source"], "Blue")

    def test_parses_canvas_size_string_without_using_tile_size_as_width(self):
        objects = [
            "$null",
            {
                "size": "{574, 1099}",
                "tileSize": 256,
                "layers": plistlib.UID(2),
            },
            {"NS.objects": []},
        ]
        self._path = _write_procreate({}, objects)

        with ProcreateFile(self._path) as pf:
            self.assertEqual((pf.canvas_width, pf.canvas_height), (574, 1099))

    def test_png_sequence_can_export_unique_source_frames_only(self):
        objects = [
            "$null",
            {
                "width": 2,
                "height": 2,
                "tileSize": 2,
                "unwrappedLayers": plistlib.UID(2),
                "animation": plistlib.UID(6),
            },
            {"NS.objects": [plistlib.UID(3), plistlib.UID(4)]},
            {"name": "Red", "uuid": "red-frame"},
            {"name": "Blue", "uuid": "blue-frame", "animationHeldLength": 2},
            "unused",
            {"frameRate": 12, "playbackMode": 2},
        ]
        self._path = _write_procreate(
            {
                "red-frame/0_0.chunk": _rgba_bytes((255, 0, 0, 255)),
                "blue-frame/0_0.chunk": _rgba_bytes((0, 0, 255, 255)),
            },
            objects,
        )
        self._out_dir = tempfile.mkdtemp()

        with ProcreateFile(self._path) as pf:
            metadata_path = pf.export_animation_png_sequence(
                self._out_dir,
                fps=12,
                expand_holds=False,
            )

        self.assertTrue(os.path.exists(os.path.join(self._out_dir, "frame_0001.png")))
        self.assertTrue(os.path.exists(os.path.join(self._out_dir, "frame_0002.png")))
        self.assertFalse(os.path.exists(os.path.join(self._out_dir, "frame_0003.png")))
        with open(metadata_path, "r", encoding="utf-8") as f:
            metadata = json.load(f)
        self.assertEqual(metadata["frame_count"], 2)
        self.assertEqual(metadata["expanded_frame_count"], 4)
        self.assertEqual(metadata["frames"][1]["hold_frames"], 2)

    def test_static_composite_for_animation_assist_shows_one_frame(self):
        objects = [
            "$null",
            {
                "width": 2,
                "height": 2,
                "tileSize": 2,
                "unwrappedLayers": plistlib.UID(2),
                "animation": plistlib.UID(9),
                "backgroundHidden": True,
            },
            {"NS.objects": [plistlib.UID(3), plistlib.UID(6)]},
            {"$class": plistlib.UID(10), "name": "Frame 1", "children": plistlib.UID(4)},
            {"NS.objects": [plistlib.UID(5)]},
            {"name": "Red", "uuid": "red-frame"},
            {"$class": plistlib.UID(10), "name": "Frame 2", "children": plistlib.UID(7)},
            {"NS.objects": [plistlib.UID(8)]},
            {"name": "Blue", "uuid": "blue-frame"},
            {"frameRate": 12},
            {"$classname": "SilicaGroup"},
        ]
        self._path = _write_procreate(
            {
                "red-frame/0_0.chunk": _rgba_bytes((255, 0, 0, 255)),
                "blue-frame/0_0.chunk": _rgba_bytes((0, 0, 255, 255)),
            },
            objects,
        )

        with ProcreateFile(self._path) as pf:
            first = pf.composite_layers()
            second = pf.composite_layers({0: False})

        self.assertEqual(first.getpixel((0, 0)), (255, 0, 0, 255))
        self.assertEqual(second.getpixel((0, 0)), (0, 0, 255, 255))

    def test_layer_tile_data_is_flipped_to_canvas_orientation(self):
        objects = [
            "$null",
            {
                "width": 2,
                "height": 2,
                "tileSize": 2,
                "layers": plistlib.UID(2),
                "backgroundHidden": True,
            },
            {"NS.objects": [plistlib.UID(3)]},
            {"name": "Layer", "uuid": "layer"},
        ]
        self._path = _write_procreate(
            {
            "layer/0_0.chunk": _rgba_pixels([
                    (0, 0, 255, 255), (0, 0, 255, 255),
                    (255, 0, 0, 255), (255, 0, 0, 255),
                ]),
            },
            objects,
        )

        with ProcreateFile(self._path) as pf:
            img = pf.composite_layers()

        self.assertEqual(img.getpixel((0, 0)), (255, 0, 0, 255))
        self.assertEqual(img.getpixel((0, 1)), (0, 0, 255, 255))

    def test_layer_tile_data_preserves_rgba_channel_order(self):
        objects = [
            "$null",
            {
                "width": 2,
                "height": 2,
                "tileSize": 2,
                "layers": plistlib.UID(2),
                "backgroundHidden": True,
            },
            {"NS.objects": [plistlib.UID(3)]},
            {"name": "Layer", "uuid": "layer"},
        ]
        self._path = _write_procreate(
            {"layer/0_0.chunk": _rgba_bytes((255, 0, 0, 255))},
            objects,
        )

        with ProcreateFile(self._path) as pf:
            img = pf.composite_layers()

        self.assertEqual(img.getpixel((0, 0)), (255, 0, 0, 255))

    def test_premultiplied_alpha_layer_edges_are_unpremultiplied(self):
        objects = [
            "$null",
            {
                "width": 2,
                "height": 2,
                "tileSize": 2,
                "layers": plistlib.UID(2),
            },
            {"NS.objects": [plistlib.UID(3)]},
            {"name": "Layer", "uuid": "layer"},
        ]
        self._path = _write_procreate(
            {"layer/0_0.chunk": _rgba_bytes((128, 0, 0, 128))},
            objects,
        )

        with ProcreateFile(self._path) as pf:
            img = pf.composite_layers()

        self.assertEqual(img.getpixel((0, 0)), (255, 127, 127, 255))

    def test_contents_rect_does_not_resample_layer_edges(self):
        red = (255, 0, 0, 255)
        empty = (0, 0, 0, 0)
        objects = [
            "$null",
            {
                "width": 4,
                "height": 4,
                "tileSize": 4,
                "layers": plistlib.UID(2),
                "backgroundHidden": True,
            },
            {"NS.objects": [plistlib.UID(3)]},
            {
                "name": "Layer",
                "uuid": "layer",
                "contentsRectValid": True,
                "contentsRect": struct_pack_doubles(0.0, 2.0, 4.0, 2.0),
            },
        ]
        self._path = _write_procreate(
            {
                "layer/0_0.chunk": _rgba_grid([
                    [empty, empty, empty, empty],
                    [empty, empty, empty, empty],
                    [red, red, empty, empty],
                    [red, red, empty, empty],
                ])
            },
            objects,
        )

        with ProcreateFile(self._path) as pf:
            img = pf.composite_layers()

        self.assertEqual(img.getpixel((0, 0)), red)
        self.assertEqual(img.getpixel((2, 0)), empty)

    def test_layer_tile_data_keeps_partial_edge_tiles(self):
        red = (255, 0, 0, 255)
        empty = (0, 0, 0, 0)
        objects = [
            "$null",
            {
                "width": 4,
                "height": 5,
                "tileSize": 4,
                "layers": plistlib.UID(2),
                "backgroundHidden": True,
            },
            {"NS.objects": [plistlib.UID(3)]},
            {"name": "Layer", "uuid": "layer"},
        ]
        self._path = _write_procreate(
            {
                "layer/0_0.chunk": _rgba_bytes(empty, size=4),
                "layer/0_1.chunk": _rgba_grid([[red, red, red, red]]),
            },
            objects,
        )

        with ProcreateFile(self._path) as pf:
            img = pf.composite_layers()

        self.assertEqual(img.getpixel((0, 0)), red)
        self.assertEqual(img.getpixel((0, 4)), empty)

    def test_folder_visibility_override_hides_children_in_composite(self):
        objects = [
            "$null",
            {
                "width": 2,
                "height": 2,
                "tileSize": 2,
                "unwrappedLayers": plistlib.UID(2),
                "backgroundHidden": True,
            },
            {"NS.objects": [plistlib.UID(3)]},
            {
                "$class": plistlib.UID(7),
                "name": "Group",
                "children": plistlib.UID(4),
            },
            {"NS.objects": [plistlib.UID(5)]},
            {"name": "Red", "uuid": "red-frame"},
            "unused",
            {"$classname": "SilicaGroup"},
        ]
        self._path = _write_procreate(
            {"red-frame/0_0.chunk": _rgba_bytes((255, 0, 0, 255))},
            objects,
        )

        with ProcreateFile(self._path) as pf:
            visible = pf.composite_layers()
            hidden = pf.composite_layers({0: False})

        self.assertEqual(visible.getpixel((0, 0)), (255, 0, 0, 255))
        self.assertIsNone(hidden)


def struct_pack_floats(*values):
    import struct
    return struct.pack("<" + "f" * len(values), *values)


def struct_pack_doubles(*values):
    import struct
    return struct.pack("<" + "d" * len(values), *values)




if __name__ == "__main__":
    unittest.main()
