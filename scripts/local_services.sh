#!/bin/bash
# Set up the local open-source services Exagion runs on. After this the app
# calls no public web service; downloads happen only here, once.
#
#   scripts/local_services.sh map <osm.pbf> [bounds]   Planetiler tiles + TileServer GL on :8089
#   scripts/local_services.sh nominatim <osm.pbf>      Nominatim on :8088 (import takes hours)
#   scripts/local_services.sh photon                   Photon on :2322, indexed from that Nominatim
#   scripts/local_services.sh app                      production build on :4174, published at / over Tailscale
#
# Valhalla (:8002) and the Overture places extract are set up separately (README).
# The backend reads PHOTON_URL (default http://127.0.0.1:2322) and NOMINATIM_SEARCH_URL.
set -euo pipefail
GEO=${GEO_DIR:-$HOME/geo}
PUBLIC_URL=${PUBLIC_URL:-https://$(tailscale status --json | python3 -c 'import json,sys;print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')}

case "${1:-}" in
map)
  PBF=$(realpath "$2"); BOUNDS=${3:--124.6,32.4,-114.0,42.1}  # default: California
  OUT=$GEO/tiles; mkdir -p "$OUT/assets/sprites" "$OUT/styles"
  docker run --rm -e JAVA_TOOL_OPTIONS="-Xmx40g" -v "$OUT:/data" -v "$PBF:/osm/input.osm.pbf:ro" \
    ghcr.io/onthegomap/planetiler:latest --osm-path=/osm/input.osm.pbf --bounds="$BOUNDS" \
    --download --output=/data/california.pmtiles --nodemap-type=array --storage=mmap
  # OpenFreeMap's Liberty style (OpenMapTiles schema) with its fonts and sprites.
  OFM=https://tiles.openfreemap.org
  cd "$OUT/assets"
  curl -fsS "$OFM/styles/liberty" -o liberty.json
  for f in ofm.json ofm.png ofm@2x.json ofm@2x.png; do curl -fsS "$OFM/sprites/ofm_f384/$f" -o "sprites/$f"; done
  for font in "Noto Sans Regular" "Noto Sans Bold" "Noto Sans Italic"; do
    mkdir -p "fonts/$font"
    for i in $(seq 0 255); do
      echo "url = \"$OFM/fonts/${font// /%20}/$((i * 256))-$((i * 256 + 255)).pbf\""
      echo "output = \"fonts/$font/$((i * 256))-$((i * 256 + 255)).pbf\""
    done
  done | curl -sS --parallel --parallel-max 16 -K -
  # Point the style at TileServer GL's local data, fonts and sprites.
  python3 - "$OUT" <<'PY'
import json, sys
out = sys.argv[1]
style = json.load(open(f"{out}/assets/liberty.json"))
style["sources"] = {"openmaptiles": {"type": "vector", "url": "pmtiles://{openmaptiles}"}}
style["layers"] = [l for l in style["layers"] if l.get("source") in (None, "openmaptiles")]  # drop raster relief
style["glyphs"] = "{fontstack}/{range}.pbf"
style["sprite"] = "ofm"
json.dump(style, open(f"{out}/styles/liberty.json", "w"))
json.dump({"options": {"paths": {"root": "/data", "fonts": "assets/fonts", "sprites": "assets/sprites",
                                 "styles": "styles", "pmtiles": "/data"}},
           "styles": {"liberty": {"style": "liberty.json"}},
           "data": {"openmaptiles": {"pmtiles": "california.pmtiles"}}},
          open(f"{out}/config.json", "w"), indent=1)
PY
  docker rm -f tileserver 2>/dev/null || true
  docker run -d --name tileserver --restart unless-stopped -v "$OUT:/data" -p 127.0.0.1:8089:8080 \
    maptiler/tileserver-gl-light:latest --config /data/config.json --public_url "$PUBLIC_URL/tiles/"
  tailscale serve --bg --set-path /tiles http://127.0.0.1:8089
  ;;
nominatim)
  PBF=$(realpath "$2"); mkdir -p "$GEO/nominatim"
  [ "$PBF" = "$GEO/nominatim/$(basename "$PBF")" ] || cp "$PBF" "$GEO/nominatim/"
  docker rm -f nominatim 2>/dev/null || true
  # Wikipedia importance ranks results; TIGER adds US house numbers.
  docker run -d --name nominatim --restart unless-stopped --shm-size=4g \
    -e PBF_PATH="/nominatim/data/$(basename "$PBF")" \
    -e IMPORT_WIKIPEDIA=true -e IMPORT_US_POSTCODES=true -e IMPORT_TIGER_ADDRESSES=true \
    -e THREADS=20 -e NOMINATIM_PASSWORD=nominatim-local \
    -e POSTGRES_SHARED_BUFFERS=8GB -e POSTGRES_MAINTENANCE_WORK_MEM=10GB -e POSTGRES_AUTOVACUUM_WORK_MEM=2GB \
    -e POSTGRES_EFFECTIVE_CACHE_SIZE=24GB -e POSTGRES_WORK_MEM=50MB -e OSM2PGSQL_CACHE=24000 \
    -v "$GEO/nominatim:/nominatim/data" -v nominatim-db:/var/lib/postgresql/16/main \
    -p 127.0.0.1:8088:8080 mediagis/nominatim:5.1
  echo "Importing; follow with: docker logs -f nominatim"
  ;;
photon)
  # Search-as-you-type and typo-tolerant names, from komoot's Photon release.
  mkdir -p "$GEO/photon" && cd "$GEO/photon"
  [ -f photon-1.3.0.jar ] || curl -fsSL -o photon-1.3.0.jar https://github.com/komoot/photon/releases/download/1.3.0/photon-1.3.0.jar
  DB_HOST=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' nominatim)
  java -Xmx16g -jar photon-1.3.0.jar import -j 16 -host "$DB_HOST" -user nominatim -password nominatim-local \
    -languages en -data-dir "$GEO/photon"
  echo "Serve with: java -Xmx8g -jar $GEO/photon/photon-1.3.0.jar serve -data-dir $GEO/photon -listen-ip 127.0.0.1"
  ;;
app)
  # The phone loads the minified build (~0.4 MB), not the dev server (~32 MB).
  cd "$(dirname "$0")/.." && npx vite build
  tailscale serve --bg --set-path /api http://127.0.0.1:5055/api
  tailscale serve --bg --set-path / http://127.0.0.1:4174
  exec npx vite preview --config vite.config.js --host 127.0.0.1 --port 4174 --strictPort
  ;;
*) sed -n 2,11p "$0"; exit 1 ;;
esac
