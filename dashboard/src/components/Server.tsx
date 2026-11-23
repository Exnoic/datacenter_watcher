import { useEffect, useState } from "react";
import type { Server as ServerType } from "./types";

export function ServerDetails({server_id}: {server_id: string}) {
  const [server, setServer] = useState<ServerType>();
  useEffect(() => {
    fetch(`/api/servers/${server_id}`).then((r) => r.json()).then(setServer);
  }, [server_id]);

  if (!server) return <p>Loading...</p>;
  return (
    <div className="server">
      <ul>
        <li>
          <div>
            <h3>Details</h3>
            <span>IP: {server.ip}</span>
            <span>Serial number: {server.serial_number}</span>
            <span>Manufacturer: {server.manufacturer}</span>
            <span>Model: {server.model}</span>
          </div>
          <div>
            <h3>Version</h3>
            <span>BIOS version: {server.bios_version}</span>
            <span>Memory: {server.memory}</span>
            <span>Processor: {server.processor_model}</span>
            <span>Power state: {server.power_state}</span>
          </div>
          <div>
            <h3>Position</h3>
            <span>Rack: rack{server.rack}</span>
            <span>Position in rack: {server.position}</span>
            <span>Zone: {server.zone}</span>
          </div>
        </li>
      </ul>
    </div>
  );
}
