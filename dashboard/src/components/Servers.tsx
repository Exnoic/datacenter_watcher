import { useEffect, useState } from "react";
import type { Server as ServerType } from "./types";
import { ServerDetails } from "./Server";

export function AllServers() {
  const [servers, setServers] = useState<ServerType[]>([]);
  const [selected, setSelected] = useState<string>();
  const [opened, setOpened] = useState<Set<string>>(new Set());
  useEffect(() => {
    fetch("/api/servers").then((r) => r.json()).then(setServers);
  }, []);

  const toggle = (id: string) => {
    setSelected(selected === id ? undefined : id);
    setOpened((prev) => new Set(prev).add(id));
  };

  return (
    <div className="server">
      <h2>Servers</h2>
      <ul>
        {servers.map((s) => (
          <li key={s.serial_number}>
            <span>{s.name} </span>
            <span>{s.health} </span>
            <span>rack{s.rack}, position {s.position} </span>
            <button
              aria-expanded={selected === s.serial_number}
              onClick={() => toggle(s.serial_number)}
            >
              {selected === s.serial_number ? "Hide" : "View server"}
            </button>

            <div
              className={`server-details${selected === s.serial_number ? " open" : ""}`}
              inert={selected !== s.serial_number}
            >
              <div className="server-details-inner">
                {opened.has(s.serial_number) && <ServerDetails server_id={s.serial_number} />}
              </div>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
