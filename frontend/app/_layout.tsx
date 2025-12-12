import React, { useEffect } from 'react';
import { Stack, useRouter, useSegments } from 'expo-router';
import { AuthProvider, useAuth } from '../contexts/AuthContext';

function RootLayoutNav() {
  const { user, loading } = useAuth();
  const segments = useSegments();
  const router = useRouter();

  useEffect(() => {
    if (loading) return;

    const inAuthGroup = segments[0] === '(auth)';

    if (!user && !inAuthGroup && segments[0] !== 'login' && segments[0] !== 'signup') {
      // Redirect to login if not authenticated
      // But allow index page
      if (segments[0] !== undefined && segments[0] !== 'index') {
        router.replace('/login');
      }
    } else if (user && (segments[0] === 'login' || segments[0] === 'signup')) {
      // Redirect away from auth pages if already logged in
      router.replace('/home');
    }
  }, [user, loading, segments]);

  return (
    <Stack
      screenOptions={{
        headerShown: false,
        contentStyle: { backgroundColor: '#0a0a0a' },
      }}
    >
      <Stack.Screen name="index" />
      <Stack.Screen name="login" />
      <Stack.Screen name="signup" />
      <Stack.Screen name="home" />
    </Stack>
  );
}

export default function RootLayout() {
  return (
    <AuthProvider>
      <RootLayoutNav />
    </AuthProvider>
  );
}