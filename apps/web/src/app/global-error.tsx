"use client";

export default function GlobalError({ reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return (
    <html lang="es">
      <body
        style={{
          margin: 0,
          display: "flex",
          minHeight: "100vh",
          alignItems: "center",
          justifyContent: "center",
          fontFamily: "system-ui, sans-serif",
          background: "#faf9fc",
          color: "#211a29",
        }}
      >
        <div style={{ maxWidth: 420, padding: "0 16px", textAlign: "center" }}>
          <h1 style={{ fontSize: "1.5rem", marginBottom: "0.75rem" }}>Algo ha ido mal</h1>
          <p style={{ color: "#5b5468", marginBottom: "1.5rem" }}>
            Ha ocurrido un error inesperado en la aplicación. Puedes intentar recargarla.
          </p>
          <button
            type="button"
            onClick={() => reset()}
            style={{
              borderRadius: 8,
              border: "none",
              background: "#7a2e5c",
              color: "#ffffff",
              padding: "0.65rem 1.5rem",
              fontSize: "0.95rem",
              fontWeight: 500,
              cursor: "pointer",
            }}
          >
            Reintentar
          </button>
        </div>
      </body>
    </html>
  );
}
