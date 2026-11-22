import type { NextConfig } from "next";

const config: NextConfig = {
  // Next.js matches the Origin hostname without its scheme or port.
  allowedDevOrigins: process.env.NEXT_DEV_ALLOWED_ORIGINS?.split(",").map((host) => host.trim()).filter(Boolean),
  async rewrites() {
    const backend = process.env.API_ORIGIN ?? "http://127.0.0.1:8000";
    return [{ source: "/api/:path*", destination: backend + "/api/:path*" }];
  },
};

export default config;
