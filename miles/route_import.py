"""`miles-route-import`: upload GPX or KML files to a running miles-api instance.

A plain HTTP client, not a direct DB writer: the files and the database
(with its derive pipeline) are often on different machines, so this always
goes over the network, even when both are on the same machine.
"""

import sys
from pathlib import Path

import click
import requests


@click.command()
@click.argument("files", nargs=-1, required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--name", default=None, help="Athlete-facing name (used as-is for a single-track file; a prefix for a multi-track file).")
@click.option("--source", default=None, help="Where this came from, e.g. alltrails, caltopo, garmin, race.")
@click.option("--notes", default=None, help="Free-text notes.")
@click.option("--tags", default=None, help="Comma-separated tags.")
@click.option("--url", default="http://localhost:8000", help="Base URL of a running miles-api instance.")
def main(files: tuple[Path, ...], name: str | None, source: str | None, notes: str | None, tags: str | None, url: str) -> None:
    for path in files:
        data = {
            "name": name or "",
            "source": source or "",
            "notes": notes or "",
            "tags": tags or "",
        }
        mime = "application/vnd.google-earth.kml+xml" if path.suffix.lower() == ".kml" else "application/gpx+xml"
        with path.open("rb") as fh:
            resp = requests.post(
                f"{url}/api/routes/upload",
                files={"file": (path.name, fh, mime)},
                data=data,
                timeout=60,
            )
        if not resp.ok:
            print(f"{path.name}: FAILED ({resp.status_code}) {resp.text[:300]}", file=sys.stderr)
            continue
        routes = resp.json()
        for r in routes:
            gain = f"{r['gain_m']:.0f}m gain" if r["gain_m"] is not None else "no elevation"
            print(f"{path.name}: route {r['route_id']} \"{r['name']}\" — {r['distance_m']:.0f}m, {gain}")


if __name__ == "__main__":
    main()
