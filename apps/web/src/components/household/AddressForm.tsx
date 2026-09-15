"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useForm } from "react-hook-form";

import { ApiError } from "@/lib/api/client";
import type { GeocodeStatus, HouseholdResponse } from "@/lib/api/types";
import {
  type HouseholdAddressFormValues,
  householdAddressSchema,
} from "@/lib/onboarding/schemas";
import { useUpdateHouseholdAddressMutation } from "@/lib/query/hooks/use-households";

import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";

export interface AddressFormProps {
  householdId: string;
  household: HouseholdResponse;
}

/** Feedback on the geocode result for the address just saved. `"disabled"` renders nothing. */
function GeocodeResult({ status }: { status: GeocodeStatus | null }) {
  if (status === "ok") {
    return (
      <Alert tone="success">
        Ubicación encontrada ✓. Ya se usará para calcular el desplazamiento a cada supermercado.
      </Alert>
    );
  }
  if (status === "not_found") {
    return (
      <Alert tone="warning">
        No pudimos localizar esa dirección; revisa el domicilio o el código postal.
      </Alert>
    );
  }
  return null;
}

/** Edits a household's address, used to price travel to nearby stores in the plan comparison (FASE 2b). */
export function AddressForm({ householdId, household }: AddressFormProps) {
  const updateAddress = useUpdateHouseholdAddressMutation(householdId);

  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<HouseholdAddressFormValues>({
    resolver: zodResolver(householdAddressSchema),
    values: {
      address_text: household.address_text ?? "",
      postal_code: household.postal_code ?? "",
      city: household.city ?? "",
    },
  });

  const onSubmit = handleSubmit(async (values) => {
    try {
      await updateAddress.mutateAsync({
        address_text: values.address_text,
        postal_code: values.postal_code ? values.postal_code : null,
        city: values.city ? values.city : null,
      });
    } catch {
      // surfaced below via updateAddress.error
    }
  });

  return (
    <form onSubmit={onSubmit} noValidate className="flex flex-col gap-4">
      {updateAddress.isError && !(updateAddress.error instanceof ApiError && updateAddress.error.status === 422) ? (
        <Alert tone="error">No se pudo guardar el domicilio. Inténtalo de nuevo.</Alert>
      ) : null}
      <Input
        label="Domicilio"
        placeholder="p. ej. Calle Mayor 12"
        hint="Calle y número. Se usa solo para calcular distancias, no se muestra a nadie más."
        required
        error={errors.address_text?.message}
        {...register("address_text")}
      />
      <Input
        label="Código postal"
        placeholder="p. ej. 14001"
        error={errors.postal_code?.message}
        {...register("postal_code")}
      />
      <Input
        label="Municipio"
        placeholder="p. ej. Córdoba"
        error={errors.city?.message}
        {...register("city")}
      />
      <Button type="submit" size="sm" loading={updateAddress.isPending} className="self-start">
        Guardar domicilio
      </Button>
      {updateAddress.isSuccess ? <GeocodeResult status={updateAddress.data.geocode_status} /> : null}
    </form>
  );
}
