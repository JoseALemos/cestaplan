import { z } from "zod";

/**
 * The only structured form field in the importer beyond the file itself:
 * an optional JSON object mapping the uploaded file's column headers to the
 * canonical CSV columns documented on `/admin/importacion` (advanced/rarely
 * used, hence its own tiny schema rather than folding into the page).
 */
export const importMappingSchema = z.object({
  column_mapping: z
    .string()
    .trim()
    .optional()
    .refine(
      (value) => {
        if (!value) return true;
        try {
          JSON.parse(value);
          return true;
        } catch {
          return false;
        }
      },
      { message: 'Debe ser JSON válido, p. ej. {"Nombre producto": "product_name"}' },
    ),
});
export type ImportMappingFormValues = z.infer<typeof importMappingSchema>;
