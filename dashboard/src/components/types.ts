// Shapes mirror the SQLModel tables in ../../models.py

export type Server = {
  serial_number: string;
  name: string;
  ip: string;
  model: string;
  health: string;
  bios_version: string;
  manufacturer: string;
  power_state: string;
  processor_model: string;
  memory: string;
  timestamp: string;
  rack: number;
  position: number;
  zone: number;
};

export type Sensor = {
  sensor_id: string;
  ip: string;
  location: string;
  rack: number;
  serial: string;
  firmware: string;
  position: number;
  zone: number;
  timestamp: string;
};

export type Temperature = {
  id: number;
  value: number | null;
  timestamp: string;
  server_serial: string | null;
  sensor_id: string | null;
};

export type Notification = {
  id: number;
  key: string;
  message: string;
  created_at: string;
  resolved_at: string | null;
};
