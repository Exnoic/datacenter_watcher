import type { Sensor, Server } from "./types";

const ZONES = [1, 2, 3] as const;

type ZoneProps = {
  zone: number;
  temperature: number | null;
};

function Zone({ zone, temperature }: ZoneProps) {
  // TODO: implement (e.g. color by temperature)

  return (
    <div className="rack-zone">
      <span>Zone {zone}</span>
      <span>{temperature ?? "–"} °C</span>
    </div>
  );
}

type RackViewProps = {
  rack: number;
  servers: Server[];
  sensors: Sensor[];
};

export function RackView({ rack, servers, sensors }: RackViewProps) {
  // TODO: implement
  void servers;
  void sensors;

  const getZoneTemperature = (zone: number): number | null => {
    // TODO: compute temperature for zone
    void zone;
    return null;
  };

  return (
    <div className="rack-view">
      <h2>Rack {rack}</h2>
      {ZONES.map((zone) => (
        <Zone key={zone} zone={zone} temperature={getZoneTemperature(zone)} />
      ))}
    </div>
  );
}
