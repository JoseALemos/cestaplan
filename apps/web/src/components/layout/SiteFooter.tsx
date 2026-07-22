import Link from "next/link";

export function SiteFooter() {
  return (
    <footer id="privacidad" className="border-t border-border bg-bg-subtle">
      <div className="mx-auto max-w-6xl px-4 py-10 sm:px-6">
        <div className="grid gap-8 sm:grid-cols-3">
          <div>
            <p className="font-display text-base font-semibold text-ink">CestaPlan</p>
            <p className="mt-2 text-sm text-ink-muted">
              Planificador de comidas de código abierto, consciente del presupuesto y de la
              tienda.
            </p>
          </div>
          <div>
            <p className="text-sm font-semibold text-ink">Producto</p>
            <ul className="mt-2 flex flex-col gap-2 text-sm text-ink-muted">
              <li>
                <Link href="/#como-funciona" className="hover:text-ink">
                  Cómo funciona
                </Link>
              </li>
              <li>
                <Link href="/#principios" className="hover:text-ink">
                  Principios
                </Link>
              </li>
              <li>
                <a
                  href="https://github.com"
                  className="hover:text-ink"
                  target="_blank"
                  rel="noreferrer"
                >
                  Código en GitHub
                </a>
              </li>
            </ul>
          </div>
          <div>
            <p className="text-sm font-semibold text-ink">Legal</p>
            <ul className="mt-2 flex flex-col gap-2 text-sm text-ink-muted">
              <li>Licencia MIT</li>
              <li>Autohospedable (self-hosted)</li>
            </ul>
          </div>
        </div>

        <p className="mt-8 max-w-3xl text-xs leading-relaxed text-ink-faint">
          CestaPlan facilita la planificación y ofrece información orientativa. No sustituye el
          consejo de un profesional sanitario. Comprueba siempre las etiquetas de los productos
          en caso de alergia o intolerancia.
        </p>

        <p className="mt-4 text-xs text-ink-faint">
          © {new Date().getFullYear()} CestaPlan. Código MIT.
        </p>
      </div>
    </footer>
  );
}
