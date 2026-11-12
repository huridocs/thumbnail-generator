# thumbnail-generator

HTTP service that turns an image into a JPEG thumbnail. The image carries libvips, libheif, libde265, and ImageMagick, so a caller whose own libvips is older than 8.18.7 can still decode HEIC and BMP.

The same container runs on a laptop, in WSL, and on a Debian VPS. The VPS case is the one where the caller is on a different machine.

## Requirements

Docker, with Compose v2.

The image is Debian forky. The build refuses to finish unless all of these are true:

- `vips --version` is 8.18.7 or newer
- that vips loads HEIF
- that vips loads images through ImageMagick (`Magick load`)
- the libheif libde265 plugin is installed, which is the HEVC decoder an iPhone photo needs

## Configure

```bash
cp .env.example .env
```

| Variable | Default | |
| --- | --- | --- |
| `BIND_HOST` | `0.0.0.0` | Address the published port binds on the host. `127.0.0.1` keeps a laptop off the LAN. Ignored by the production file, which uses the host network. |
| `PORT` | `6020` | Host port. Inside the bridge compose the process always listens on 6020. |
| `MAX_BYTES` | `41943040` | Largest accepted request body, 40 MB. |
| `TIMEOUT_SECONDS` | `60` | Limit for one `vips` command, and for waiting on a free worker. |
| `MAX_CONCURRENCY` | `2` | How many thumbnails run at once. |

## Run locally

```bash
docker compose up --build -d
curl -fsS http://127.0.0.1:6020/health
```

Call `http://127.0.0.1:6020`, not `http://localhost`. On WSL, `localhost` often resolves to IPv6 `::1`, and Docker publishes the port on IPv4.

Docker will not take a port the host already holds. If `6020` is taken, set `PORT` in `.env`.

A caller on the same machine uses `http://127.0.0.1:6020`.

If `docker compose build` cannot resolve `deb.debian.org`, the daemon's DNS is the usual WSL failure. Write `/etc/docker/daemon.json`:

```json
{
  "dns": ["1.1.1.1", "8.8.8.8"]
}
```

Restart the daemon (`sudo service docker restart`) and build again. The build already uses the host network so it can reach the archive.

`BIND_HOST=0.0.0.0` is what makes the port reachable from Windows when Docker is the WSL engine, and from another container through the host. Set `BIND_HOST=127.0.0.1` when the laptop should not listen on the LAN. A caller on another machine cannot use that setting.

## Run on a Debian VPS

The caller is on a different IP, so this file puts the process on the host network. The firewall then sees the caller's address. Docker's normal port publishing would hide that address, and on Debian it also accepts the port before ufw runs.

```bash
cp .env.example .env
# set PORT if 6020 is taken
docker compose -f docker-compose.production.yml up --build -d
```

Allow only the caller's host:

```bash
sudo ufw allow from <caller-ip> to any port 6020 proto tcp
sudo ufw deny 6020/tcp
```

`ufw deny` after the allow keeps every other address out. Reload ufw if it was already running. From the caller:

```bash
curl -fsS http://<this-host>:6020/health
```

## API

`GET /health` returns the `vips --version` line, `text/plain`.

`POST /thumbnails` sends the original bytes as the body and returns `image/jpeg`.

| Query | Default | Allowed |
| --- | --- | --- |
| `edge` | `640` | `1`–`4096` |
| `quality` | `60` | `1`–`100` |
| `maxPixels` | `268402689` | `1`–`268402689` |

A value above the pixel ceiling is rejected. The service will not raise it.

```bash
curl -fsS \
  --data-binary @photo.heic \
  "http://127.0.0.1:6020/thumbnails?edge=640&quality=60&maxPixels=268402689" \
  -o thumb.jpg
```

| Status | Meaning |
| --- | --- |
| `200` | JPEG body. |
| `400` | Empty body, or a query value outside the range above. |
| `413` | Body larger than `MAX_BYTES`. |
| `422` | The image could not be decoded, or it is over the pixel ceiling. Do not retry that file. |
| `503` | `vips` is missing, a command timed out, or every worker is busy. Retry. |

The thumbnail fits inside `edge` on the longest side and is not enlarged. Orientation is applied. An image with an alpha channel is flattened onto white. An image without one, including an iPhone HEIC, is copied to the JPEG. `vips thumbnail` straight to JPEG paints transparency black, so that path is not used.

## Checks

The image build already renders a PNG with no alpha, a PNG with alpha, and a BMP. Before a caller depends on a new tag, also run one real iPhone photo:

```bash
curl -fsS \
  --data-binary @photo.heic \
  "http://<this-host>:6020/thumbnails" \
  -o thumb.jpg
file thumb.jpg
```

`file` should report a JPEG.

## Tests

The unit tests do not need Docker:

```bash
python3 tests/test_server.py -v
```

## Develop without the container

`python3 server.py` listens on `0.0.0.0:$PORT`. It exits unless `vips` on `PATH` is 8.18.7 or newer and was built with HEIF, Magick, and the libde265 plugin. That binary is what the image is for. The host package on Debian stable is older, and this process will not use it.
