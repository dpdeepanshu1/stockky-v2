

export function BackendStatusDot({
  status,
  onClick,
}: {
  status: "checking" | "up" | "down";
  onClick: () => void;
}) {
  const label = status === "up" ? "Backend connected" : status === "down" ? "Backend unreachable" : "Checking…";
  return (
    <button
      onClick={onClick}
      title={label}
      className={`status-chip ${status === "up" ? "ok" : status === "down" ? "bad" : "warn"}`}
    >
      <span className="dot" />
      {label}
    </button>
  );
}
