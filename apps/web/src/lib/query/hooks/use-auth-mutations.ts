"use client";

import { useMutation } from "@tanstack/react-query";

import { login, logout, registerUser } from "@/lib/api/endpoints";
import { useInvalidateAuth } from "@/lib/auth/auth-context";

export function useLoginMutation() {
  const invalidateAuth = useInvalidateAuth();
  return useMutation({
    mutationFn: login,
    onSuccess: () => invalidateAuth(),
  });
}

export function useRegisterMutation() {
  return useMutation({ mutationFn: registerUser });
}

export function useLogoutMutation() {
  const invalidateAuth = useInvalidateAuth();
  return useMutation({
    mutationFn: logout,
    onSuccess: () => invalidateAuth(),
  });
}
