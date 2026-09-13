import type { MetadataRoute } from "next";

const BASE_URL = process.env.WEB_PUBLIC_URL ?? "http://localhost:3000";

// Solo rutas públicas (accesibles sin sesión iniciada); el resto de la app
// requiere autenticación y no debe indexarse.
const PUBLIC_ROUTES = ["/", "/login", "/registro", "/recuperar-contrasena", "/privacidad"];

export default function sitemap(): MetadataRoute.Sitemap {
  return PUBLIC_ROUTES.map((route) => ({
    url: `${BASE_URL}${route}`,
    lastModified: new Date(),
  }));
}
