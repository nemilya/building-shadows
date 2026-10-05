"""Запуск: python -m shadows [--host 127.0.0.1] [--port 8000] [--demo] [--file buildings.geojson]"""

import argparse
import logging
import os

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Карта теней от зданий")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--demo", action="store_true", help="синтетический квартал без сети")
    parser.add_argument("--file", help="GeoJSON со зданиями (например, выгрузка Overture Maps)")
    args = parser.parse_args()

    if args.demo:
        os.environ["SHADOWS_SOURCE"] = "demo"
    elif args.file:
        os.environ["SHADOWS_SOURCE"] = "file"
        os.environ["SHADOWS_BUILDINGS_FILE"] = args.file

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    print(f"Откройте http://{args.host}:{args.port}/")
    uvicorn.run("shadows.api:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
