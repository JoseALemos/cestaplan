"use client";

import { useMutation } from "@tanstack/react-query";

import { clearCsrfToken, storeCsrfToken } from "@/lib/api/client";
import { login, logout, registerUser } from "@/lib/api/endpoints";
import { useInvalidateAuth } from "@/lib/auth/auth-context";

export function useLoginMutation() {
  const invalidateAuth = useInvalidateAuth();
  return useMutation({
    mutationFn: login,
    onSuccess: (data) => {
      // Persist the token from the response body so mutations work even when the
      // web and API are on different origins (the CSRF cookie is unreadable then).
      storeCsrfToken(data.csrf_token);
      invalidateAuth();
    },
  });
}

export function useRegisterMutation() {
  return useMutation({ mutationFn: registerUser });
}

export function useLogoutMutation() {
  const invalidateAuth = useInvalidateAuth();
  return useMutation({
    mutationFn: logout,
    onSuccess: () => {
      clearCsrfToken();
      invalidateAuth();
    },
  });
}
