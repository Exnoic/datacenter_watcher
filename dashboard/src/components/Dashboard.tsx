import { Notifications } from "./Notifications";
import { AllSensors } from "./Sensors";
import { AllServers } from "./Servers";

export function Dashboard() {
  // TODO: fetch data

  return (
    <section className="dashboard">
      <h2>Dashboard</h2>
      <Notifications />
      <AllServers />
      <AllSensors />
    </section>
  );
}
