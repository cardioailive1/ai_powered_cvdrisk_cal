// Prisma 7 configuration. Plain ESM (no TypeScript toolchain needed on Render's Python runtime).
// DATABASE_URL is provided by Render from the cardioai-db database (render.yaml).
import { defineConfig } from "prisma/config";

export default defineConfig({
  schema: "prisma/schema.prisma",
  migrations: { path: "prisma/migrations" },
  datasource: { url: process.env.DATABASE_URL },
});
