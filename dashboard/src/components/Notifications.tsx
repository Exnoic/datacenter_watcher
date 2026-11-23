import type { Notification } from "./types";
import { useCallback, useEffect, useState } from "react";

export function Notifications() {
  const [notifications, setNotifications] = useState<Notification[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await fetch("/api/notifications");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setNotifications(await res.json());
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    }
  }, [])
  useEffect(() => {
    load();
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
  }, [load]);
  async function handleResolve (notify_key: string) {
    const res = await fetch(`/api/notification/${encodeURIComponent(notify_key)}/resolve`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
    })
    if (!res.ok) {
      const error = await res.json().catch(() => ({}));
      throw new Error(error.detal ?? `HTTP ${res.status}`);
    }
    await load();
  };
  if (error) return <p>Error: {error}</p>;
  if (notifications.length === 0) return <p>No active notifications</p>;
  return (
    <section className="notifications">
      <h2>Notifications</h2>
      <ul>
        {notifications.map((n) => (
          <li key={n.id}>
            <span>{n.message}</span>
            <button type="button" onClick={() => handleResolve(n.key)}>
              Resolve
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
