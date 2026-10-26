import argparse
import json
import random
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from awscrt import mqtt
from awsiot import mqtt_connection_builder

CERTS = Path(__file__).parent / "certs"


def make_reading(sensor, shipment, seq, mode):
    # normal range for demo product: 2-8 C, 35-65 % humidity
    temp = random.uniform(3.5, 6.5)
    hum = random.uniform(40, 55)
    if mode == "hot":
        temp = random.uniform(9.5, 12.0)
    elif mode == "cold":
        temp = random.uniform(-2.0, 0.5)
    elif mode == "humid":
        hum = random.uniform(75, 90)

    return {
        "message_id": str(uuid.uuid4()),  # unique ID, used for dedupe in Step 6
        "seq": seq,                       # per-device counter, used for ordering
        "device_id": sensor,
        "shipment_id": shipment,
        "temperature_c": round(temp, 2),
        "humidity_pct": round(hum, 1),
        "battery_pct": max(5, 95 - seq),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--endpoint", required=True)
    p.add_argument("--sensor", default="sensor-01")
    p.add_argument("--shipment", default="SHP-1001")
    p.add_argument("--count", type=int, default=5)
    p.add_argument("--interval", type=float, default=2.0)
    p.add_argument("--mode", choices=["normal", "hot", "cold", "humid"], default="normal")
    # test switches for the negative security tests
    p.add_argument("--client-id", help="override client ID (test: should be refused)")
    p.add_argument("--topic-thing", help="publish to another thing's topic (test: should be denied)")
    args = p.parse_args()

    thing = f"coldchain-{args.sensor}"
    client_id = args.client_id or thing  # must equal thing name or policy refuses connect
    topic = f"coldchain/{args.topic_thing or thing}/telemetry"

    conn = mqtt_connection_builder.mtls_from_path(
        endpoint=args.endpoint,
        cert_filepath=str(CERTS / f"{args.sensor}.cert.pem"),
        pri_key_filepath=str(CERTS / f"{args.sensor}.private.key"),
        ca_filepath=str(CERTS / "AmazonRootCA1.pem"),
        client_id=client_id,
        clean_session=False,
        keep_alive_secs=30,
        on_connection_interrupted=lambda connection, error, **kw: print(f"CONNECTION DROPPED: {error}"),
    )

    print(f"connecting as {client_id} ...")
    conn.connect().result(timeout=15)
    print(f"connected. publishing to {topic}")

    try:
        for seq in range(1, args.count + 1):
            msg = make_reading(args.sensor, args.shipment, seq, args.mode)
            future, _ = conn.publish(topic, json.dumps(msg), mqtt.QoS.AT_LEAST_ONCE)
            future.result(timeout=10)  # waits for broker confirmation (QoS 1)
            print(f"sent seq={seq} temp={msg['temperature_c']} hum={msg['humidity_pct']}")
            time.sleep(args.interval)
    except Exception as e:
        print(f"publish failed: {e}")
    finally:
        conn.disconnect().result(timeout=10)
        print("disconnected")


if __name__ == "__main__":
    main()