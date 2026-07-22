"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

import { useAuth } from "@/lib/auth/auth-context";
import { useCurrentHouseholdId } from "@/lib/household/current-household";
import { useHouseholdsQuery } from "@/lib/query/hooks/use-households";
import {
  useAddPantryItemMutation,
  useDeletePantryItemMutation,
  useIngredientsQuery,
  usePantryQuery,
  useUpdatePantryItemMutation,
} from "@/lib/query/hooks/use-pantry";
import { formatDate, formatQuantity } from "@/lib/utils/format";
import { ApiError } from "@/lib/api/client";
import { PANTRY_UNITS } from "@/lib/api/types";
import type { PantryItemResponse } from "@/lib/api/types";

import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/Card";
import { Input } from "@/components/ui/Input";
import { Select } from "@/components/ui/Select";
import { Skeleton } from "@/components/ui/Skeleton";
import { useToast } from "@/components/ui/Toast";

const UNIT_OPTIONS = PANTRY_UNITS.map((unit) => ({ value: unit, label: unit }));

function apiErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiError) {
    const body = error.body as { detail?: unknown } | null;
    if (body && typeof body.detail === "string") return body.detail;
    if (error.status === 422) return "Revisa los datos: cantidad, unidad o ingrediente.";
  }
  return fallback;
}

// --------------------------------------------------------------------------- //
// Add-item form
// --------------------------------------------------------------------------- //
function AddPantryItemForm({ householdId }: { householdId: string }) {
  const { showToast } = useToast();
  const addMutation = useAddPantryItemMutation(householdId);

  const [name, setName] = useState("");
  const [quantity, setQuantity] = useState("");
  const [unit, setUnit] = useState<string>("g");
  const [expiresAt, setExpiresAt] = useState("");
  const [error, setError] = useState<string | null>(null);

  const suggestionsQuery = useIngredientsQuery(name);
  const suggestions = suggestionsQuery.data ?? [];

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    const trimmedName = name.trim();
    const numericQuantity = Number.parseFloat(quantity);
    if (!trimmedName) {
      setError("Escribe el nombre del ingrediente.");
      return;
    }
    if (!Number.isFinite(numericQuantity) || numericQuantity <= 0) {
      setError("La cantidad debe ser mayor que cero.");
      return;
    }
    try {
      await addMutation.mutateAsync({
        name: trimmedName,
        quantity: quantity.trim(),
        unit,
        expires_at: expiresAt ? expiresAt : null,
      });
      setName("");
      setQuantity("");
      setExpiresAt("");
      showToast({ tone: "success", title: "Añadido a la despensa" });
    } catch (err) {
      setError(apiErrorMessage(err, "No se pudo añadir el artículo."));
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>Añadir a la despensa</CardTitle>
        <CardDescription>
          Elige un ingrediente conocido, indica cuánto tienes y (si quieres) su caducidad.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={submit} className="flex flex-col gap-4">
          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <Input
                label="Ingrediente"
                required
                list="pantry-ingredient-list"
                placeholder="Ej. tomate"
                value={name}
                onChange={(event) => setName(event.target.value)}
                hint="Empieza a escribir para ver sugerencias."
              />
              <datalist id="pantry-ingredient-list">
                {suggestions.map((ingredient) => (
                  <option key={ingredient.canonical_name} value={ingredient.display_name} />
                ))}
              </datalist>
            </div>
            <Input
              label="Cantidad"
              required
              type="number"
              min="0"
              step="any"
              inputMode="decimal"
              placeholder="Ej. 500"
              value={quantity}
              onChange={(event) => setQuantity(event.target.value)}
            />
          </div>
          <div className="grid gap-4 sm:grid-cols-2">
            <Select
              label="Unidad"
              options={UNIT_OPTIONS}
              value={unit}
              onChange={(event) => setUnit(event.target.value)}
            />
            <Input
              label="Caducidad (opcional)"
              type="date"
              value={expiresAt}
              onChange={(event) => setExpiresAt(event.target.value)}
            />
          </div>
          {error ? <Alert tone="error">{error}</Alert> : null}
          <div>
            <Button type="submit" loading={addMutation.isPending}>
              Añadir
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}

// --------------------------------------------------------------------------- //
// One pantry row (view + inline edit)
// --------------------------------------------------------------------------- //
function PantryRow({ householdId, item }: { householdId: string; item: PantryItemResponse }) {
  const { showToast } = useToast();
  const updateMutation = useUpdatePantryItemMutation(householdId);
  const deleteMutation = useDeletePantryItemMutation(householdId);

  const [editing, setEditing] = useState(false);
  const [quantity, setQuantity] = useState(item.quantity);
  const [unit, setUnit] = useState(item.unit);
  const [error, setError] = useState<string | null>(null);

  const startEdit = () => {
    setQuantity(item.quantity);
    setUnit(item.unit);
    setError(null);
    setEditing(true);
  };

  const save = async () => {
    setError(null);
    const numericQuantity = Number.parseFloat(quantity);
    if (!Number.isFinite(numericQuantity) || numericQuantity <= 0) {
      setError("La cantidad debe ser mayor que cero.");
      return;
    }
    try {
      await updateMutation.mutateAsync({
        itemId: item.id,
        body: { quantity: quantity.trim(), unit },
      });
      setEditing(false);
      showToast({ tone: "success", title: "Actualizado" });
    } catch (err) {
      setError(apiErrorMessage(err, "No se pudo actualizar."));
    }
  };

  const remove = async () => {
    try {
      await deleteMutation.mutateAsync(item.id);
      showToast({ tone: "success", title: "Eliminado de la despensa" });
    } catch {
      showToast({ tone: "error", title: "No se pudo eliminar." });
    }
  };

  return (
    <li className="flex flex-col gap-3 rounded-md border border-border px-4 py-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="text-sm font-medium text-ink">{item.display}</p>
          {editing ? null : (
            <p className="text-xs text-ink-muted">
              {formatQuantity(item.quantity, item.unit)}
              {item.expires_at ? ` · caduca ${formatDate(item.expires_at)}` : ""}
            </p>
          )}
        </div>
        {editing ? null : (
          <div className="flex items-center gap-2">
            {item.expires_at ? <Badge tone="neutral">Caduca {formatDate(item.expires_at)}</Badge> : null}
            <Button type="button" variant="ghost" size="sm" onClick={startEdit}>
              Editar
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              loading={deleteMutation.isPending}
              onClick={remove}
            >
              Quitar
            </Button>
          </div>
        )}
      </div>

      {editing ? (
        <div className="flex flex-col gap-3">
          <div className="grid gap-3 sm:grid-cols-2">
            <Input
              label="Cantidad"
              type="number"
              min="0"
              step="any"
              inputMode="decimal"
              value={quantity}
              onChange={(event) => setQuantity(event.target.value)}
            />
            <Select
              label="Unidad"
              options={UNIT_OPTIONS}
              value={unit}
              onChange={(event) => setUnit(event.target.value)}
            />
          </div>
          {error ? <Alert tone="error">{error}</Alert> : null}
          <div className="flex items-center gap-2">
            <Button type="button" size="sm" loading={updateMutation.isPending} onClick={save}>
              Guardar
            </Button>
            <Button type="button" variant="ghost" size="sm" onClick={() => setEditing(false)}>
              Cancelar
            </Button>
          </div>
        </div>
      ) : null}
    </li>
  );
}

// --------------------------------------------------------------------------- //
// Page
// --------------------------------------------------------------------------- //
export default function DespensaPage() {
  const router = useRouter();
  const { isAuthenticated, isLoading: authLoading } = useAuth();
  const householdsQuery = useHouseholdsQuery();
  const [householdId, setHouseholdId] = useCurrentHouseholdId();

  useEffect(() => {
    if (!authLoading && !isAuthenticated) {
      router.replace("/login");
    }
  }, [authLoading, isAuthenticated, router]);

  const households = useMemo(() => householdsQuery.data ?? [], [householdsQuery.data]);

  // Default to the first household if none is selected yet.
  useEffect(() => {
    const first = households[0];
    if (!householdId && first) {
      setHouseholdId(first.id);
    }
  }, [householdId, households, setHouseholdId]);

  const pantryQuery = usePantryQuery(householdId);
  const items = pantryQuery.data ?? [];

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-6 px-4 py-10 sm:px-6">
      <div>
        <h1 className="font-display text-display-lg text-ink">Despensa</h1>
        <p className="mt-2 text-ink-muted">
          Registra lo que ya tienes en casa. En tu próximo plan, CestaPlan comprará menos de
          esos ingredientes, reduciendo la lista de la compra y el coste.
        </p>
      </div>

      {households.length > 1 ? (
        <Card>
          <CardContent>
            <Select
              label="Hogar"
              options={households.map((household) => ({ value: household.id, label: household.name }))}
              value={householdId ?? ""}
              onChange={(event) => setHouseholdId(event.target.value)}
            />
          </CardContent>
        </Card>
      ) : null}

      {householdsQuery.isLoading ? (
        <Skeleton className="h-24 w-full" />
      ) : households.length === 0 ? (
        <Alert tone="info" title="Primero crea un hogar">
          Necesitas un hogar para gestionar su despensa.{" "}
          <Link href="/onboarding/hogar" className="font-medium underline">
            Crear hogar
          </Link>
        </Alert>
      ) : !householdId ? (
        <Alert tone="info" title="Selecciona un hogar">
          Elige un hogar para ver y gestionar su despensa.{" "}
          <Link href="/households" className="font-medium underline">
            Ir a hogares
          </Link>
        </Alert>
      ) : (
        <>
          <AddPantryItemForm householdId={householdId} />

          <Card>
            <CardHeader>
              <CardTitle>En tu despensa</CardTitle>
              <CardDescription>Lo que ya tienes y no hará falta comprar de nuevo.</CardDescription>
            </CardHeader>
            <CardContent>
              {pantryQuery.isLoading ? (
                <div className="flex flex-col gap-2">
                  <Skeleton className="h-14 w-full" />
                  <Skeleton className="h-14 w-full" />
                </div>
              ) : pantryQuery.isError ? (
                <Alert tone="error">No se pudo cargar la despensa. Comprueba tu conexión.</Alert>
              ) : items.length === 0 ? (
                <Alert tone="info">
                  Tu despensa está vacía. Añade lo que ya tengas en casa para ahorrar en el
                  próximo plan.
                </Alert>
              ) : (
                <ul className="flex flex-col gap-2">
                  {items.map((item) => (
                    <PantryRow key={item.id} householdId={householdId} item={item} />
                  ))}
                </ul>
              )}
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}
