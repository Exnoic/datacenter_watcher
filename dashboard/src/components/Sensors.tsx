import { useEffect, useState } from "react";
import type { Sensor as SensorType } from "./types";

export function AllSensors() {
  const [sensors, setSensors] = useState<SensorType[]>([]);

  useEffect(() => {
    fetch("/api/sensors").then((r) => r.json()).then(setSensors);
  }, []);

  return (
    <div className="server">
      <h2>Servers</h2>
      <ul>
        {sensors.map((s) => (
          <li key={s.sensor_id}>
            <span>{s.location} </span>
            <span>rack{s.rack}, position {s.position} </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
