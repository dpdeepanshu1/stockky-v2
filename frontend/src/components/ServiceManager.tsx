import { useEffect, useState } from "react";
import { api, wakeService, SystemServiceStatus } from "../api";

interface ServiceManagerProps {
  onClose: () => void;
}

export default function ServiceManager({ onClose }: ServiceManagerProps) {
  const [services, setServices] = useState<Record<string, SystemServiceStatus>>({});
  const [loading, setLoading] = useState(true);
  const [waking, setWaking] = useState<Record<string, boolean>>({});
  const [messages, setMessages] = useState<Record<string, string>>({});
  const [isWakingAll, setIsWakingAll] = useState(false);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);

  const fetchServices = async () => {
    setLoading(true);
    try {
      const health = await api.systemHealth();
      setServices(health.services);
    } catch (error) {
      console.error("Failed to fetch services", error);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchServices();
  }, []);

  const handleWake = async (name: string, url: string | null | undefined) => {
    if (!url) return;
    setWaking((prev) => ({ ...prev, [name]: true }));
    setMessages((prev) => ({ ...prev, [name]: "⏳ Waking..." }));

    try {
      await wakeService(url);
      setMessages((prev) => ({ ...prev, [name]: "✅ Woke! Rechecking..." }));
      await new Promise((resolve) => setTimeout(resolve, 3000));
      await fetchServices();
      setMessages((prev) => ({ ...prev, [name]: "✅ Online" }));
      setStatusMessage(`✅ ${name} woke successfully!`);
    } catch {
      setMessages((prev) => ({ ...prev, [name]: "❌ Failed" }));
      setStatusMessage(`❌ Failed to wake ${name}`);
    } finally {
      setWaking((prev) => ({ ...prev, [name]: false }));
      setTimeout(() => {
        setMessages((prev) => {
          const newMsg = { ...prev };
          delete newMsg[name];
          return newMsg;
        });
      }, 5000);
      setTimeout(() => setStatusMessage(null), 5000);
    }
  };

  const handleWakeAll = async () => {
    if (isWakingAll) return;
    setIsWakingAll(true);
    setStatusMessage("⏳ Waking all services...");

    const entries = Object.entries(services);
    let successCount = 0;
    let failCount = 0;

    for (const [name, status] of entries) {
      if (status.url && !status.ok) {
        try {
          await wakeService(status.url);
          successCount++;
          setStatusMessage(`⏳ Woke ${name} (${successCount}/${entries.filter(([_, s]) => s.url && !s.ok).length})`);
          await new Promise((resolve) => setTimeout(resolve, 1000));
        } catch {
          failCount++;
        }
      }
    }

    setStatusMessage(`✅ Wake complete: ${successCount} services woke, ${failCount} failed`);
    await fetchServices();
    setIsWakingAll(false);
    setTimeout(() => setStatusMessage(null), 5000);
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/80 backdrop-blur-sm p-4">
      <div className="bg-graphite border border-slate/60 rounded-2xl max-w-2xl w-full max-h-[80vh] overflow-y-auto p-6 shadow-2xl">
        <div className="flex items-center justify-between mb-6">
          <h2 className="font-display text-2xl text-paper">🛠️ Service Manager</h2>
          <button onClick={onClose} className="font-mono text-xs text-mist hover:text-paper transition">
            ✕ Close
          </button>
        </div>

        {statusMessage && (
          <div className="mb-4 font-mono text-xs text-signal-prepare animate-pulse">
            {statusMessage}
          </div>
        )}

        {loading ? (
          <div className="text-center py-8 text-mist/40 font-mono">Loading services...</div>
        ) : (
          <>
            <div className="flex justify-end mb-4">
              <button
                onClick={handleWakeAll}
                disabled={isWakingAll}
                className="font-mono text-xs border border-signal-prepare/40 text-signal-prepare px-4 py-1.5 rounded-lg hover:bg-signal-prepare/10 transition disabled:opacity-50"
              >
                {isWakingAll ? "⏳ Waking..." : "Wake All Services"}
              </button>
            </div>
            <div className="space-y-2">
              {Object.entries(services).map(([name, status]) => {
                const isWaking = waking[name] || false;
                const msg = messages[name] || "";
                return (
                  <div
                    key={name}
                    className="flex items-center justify-between border border-slate/40 rounded-lg px-4 py-3 bg-ink/60"
                  >
                    <div>
                      <span className="font-mono text-sm text-paper">{name}</span>
                      <span className="ml-2 font-mono text-[10px] text-mist/50">
                        {status.required ? "required" : "optional"}
                      </span>
                      <div className="flex items-center gap-2 mt-1">
                        <span
                          className={`font-mono text-xs ${
                            status.ok ? "text-signal-buy" : "text-signal-sell"
                          }`}
                        >
                          {status.ok ? "✅ Online" : "❌ Offline"}
                        </span>
                        {status.seconds && (
                          <span className="font-mono text-[10px] text-mist/40">
                            {status.seconds}s
                          </span>
                        )}
                      </div>
                    </div>
                    <div className="flex items-center gap-2">
                      {status.url && !status.ok && (
                        <button
                          onClick={() => handleWake(name, status.url)}
                          disabled={isWaking}
                          className="font-mono text-xs px-3 py-1.5 border border-slate rounded hover:border-mist hover:text-paper transition disabled:opacity-50"
                        >
                          {isWaking ? "⏳ Waking..." : "Wake"}
                        </button>
                      )}
                      {msg && (
                        <span className="text-[10px] text-mist/60">{msg}</span>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
            <div className="mt-4 text-xs text-mist/40 font-mono">
              Click "Wake" to open the service's health endpoint in a small window, triggering a cold start.
            </div>
          </>
        )}
      </div>
    </div>
  );
}