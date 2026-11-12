import threading
import unittest
import urllib.request
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server


class ScriptedRun:
    def __init__(self, bands="3", width="10", height="10", fail_on=None):
        self.calls = []
        self.bands = bands
        self.width = width
        self.height = height
        self.fail_on = fail_on

    def __call__(self, args, timeout):
        self.calls.append(args)
        if self.fail_on and self.fail_on in args:
            raise server.Rejected("decoder rejected the image")
        if args[:2] == ["vips", "--version"]:
            return "vips-8.18.7\n"
        if args[:2] == ["vipsheader", "-f"]:
            field = args[2]
            if field == "bands":
                return f"{self.bands}\n"
            if field == "width":
                return f"{self.width}\n"
            if field == "height":
                return f"{self.height}\n"
        if args[:2] in (["vips", "copy"], ["vips", "flatten"]):
            output = args[3].split("[", 1)[0]
            Path(output).write_bytes(b"jpeg-bytes")
        return ""


def config():
    return server.Config(
        max_bytes=1000,
        timeout=5,
        max_concurrency=2,
        port=0,
    )


def post(body, run, query=""):
    target = "/thumbnails" if not query else f"/thumbnails?{query}"
    return server.handle_request("POST", target, body, config(), run)


class PipelineTests(unittest.TestCase):
    def test_copies_a_thumbnail_that_has_no_alpha(self):
        run = ScriptedRun(bands="3")
        status, content_type, body = post(b"png-bytes", run)

        self.assertEqual(status, 200)
        self.assertEqual(content_type, "image/jpeg")
        self.assertEqual(body, b"jpeg-bytes")
        operations = [call[1] for call in run.calls if call[0] == "vips"]
        self.assertEqual(operations, ["thumbnail", "copy"])
        copy_target = run.calls[-1][-1]
        self.assertIn("[Q=60]", copy_target)
        thumbnail = next(call for call in run.calls if call[1] == "thumbnail")
        self.assertEqual(thumbnail[4:9], ["640", "--height", "640", "--size", "down"])

    def test_flattens_four_bands_onto_white(self):
        run = ScriptedRun(bands="4")
        status, _, _ = post(b"png-bytes", run, query="quality=45")

        self.assertEqual(status, 200)
        flatten = next(call for call in run.calls if call[1] == "flatten")
        self.assertEqual(flatten[-2:], ["--background", "255,255,255"])
        self.assertIn("[Q=45]", flatten[3])

    def test_flattens_grey_plus_alpha_with_one_background_value(self):
        run = ScriptedRun(bands="2")
        post(b"png-bytes", run)

        flatten = next(call for call in run.calls if call[1] == "flatten")
        self.assertEqual(flatten[-1], "255")

    def test_rejects_an_image_over_the_pixel_ceiling(self):
        run = ScriptedRun(width="20000", height="20000")
        status, content_type, body = post(b"png-bytes", run)

        self.assertEqual(status, 422)
        self.assertEqual(content_type, "text/plain")
        self.assertIn(b"pixel ceiling", body)
        self.assertFalse(any(call[1] == "thumbnail" for call in run.calls))

    def test_returns_422_when_vips_rejects_the_file(self):
        run = ScriptedRun(fail_on="thumbnail")
        status, _, body = post(b"png-bytes", run)

        self.assertEqual(status, 422)
        self.assertIn(b"decoder rejected", body)

    def test_honours_edge_and_refuses_a_raised_pixel_ceiling(self):
        run = ScriptedRun()
        status, _, _ = post(b"png-bytes", run, query="edge=320")
        thumbnail = next(call for call in run.calls if call[1] == "thumbnail")
        self.assertEqual(status, 200)
        self.assertEqual(thumbnail[4], "320")

        refused, _, message = post(b"png-bytes", ScriptedRun(), query="maxPixels=999999999")
        self.assertEqual(refused, 400)
        self.assertIn(b"maxPixels", message)


class HttpTests(unittest.TestCase):
    def test_health_returns_the_vips_version(self):
        run = ScriptedRun()
        status, content_type, body = server.handle_request("GET", "/health", b"", config(), run)

        self.assertEqual(status, 200)
        self.assertEqual(content_type, "text/plain")
        self.assertEqual(body, b"vips-8.18.7\n")

    def test_rejects_an_empty_body_and_a_body_over_the_cap(self):
        empty, _, _ = post(b"", ScriptedRun())
        huge, _, _ = server.handle_request(
            "POST",
            "/thumbnails",
            b"x" * 1001,
            config(),
            ScriptedRun(),
        )

        self.assertEqual(empty, 400)
        self.assertEqual(huge, 413)

    def test_serves_a_real_socket_on_ipv4_loopback(self):
        run = ScriptedRun()
        httpd = server.serve(config(), run, host="127.0.0.1")
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            port = httpd.server_address[1]
            response = urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2)
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"vips-8.18.7\n")
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/thumbnails",
                data=b"png-bytes",
                method="POST",
            )
            thumbnail = urllib.request.urlopen(request, timeout=2)
            self.assertEqual(thumbnail.headers.get("Content-Type"), "image/jpeg")
            self.assertEqual(thumbnail.read(), b"jpeg-bytes")
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
