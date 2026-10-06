// group 182: self-check for normalizeGatewayUrl (the frontend has no test runner).
// Run:  cd frontend && npx tsx scripts/check_gateway_url.ts   (exits 1 on any mismatch)
import { normalizeGatewayUrl } from "../src/api";

const cases: Array<[string, string]> = [
  ["", ""],
  ["   ", ""],
  ["https://gw.example.com", "https://gw.example.com"],
  ["https://gw.example.com/", "https://gw.example.com"],
  ["  https://gw.example.com///  ", "https://gw.example.com"],
  ["http://localhost:8000", "http://localhost:8000"],
  ["my-vm.duckdns.org", "https://my-vm.duckdns.org"],
  ["my-vm.duckdns.org/", "https://my-vm.duckdns.org"],
  ["my-vm.duckdns.org/api", "https://my-vm.duckdns.org/api"],
  ["//my-vm.duckdns.org", "https://my-vm.duckdns.org"],
  ["localhost:8000", "http://localhost:8000"],
  ["127.0.0.1:8000", "http://127.0.0.1:8000"],
  ["192.168.1.20:8000", "http://192.168.1.20:8000"],
  ["10.0.0.5", "http://10.0.0.5"],
  ["172.16.4.1", "http://172.16.4.1"],
  ["172.32.4.1", "https://172.32.4.1"],
  ["box.local", "http://box.local"],
  ["HTTPS://GW.Example.com", "HTTPS://GW.Example.com"],
];

let bad = 0;
for (const [input, want] of cases) {
  const got = normalizeGatewayUrl(input);
  if (got !== want) {
    bad++;
    console.error(`FAIL  ${JSON.stringify(input)} -> ${JSON.stringify(got)} (want ${JSON.stringify(want)})`);
  }
}
if (bad) process.exit(1);
console.log(`ok: ${cases.length} cases`);
