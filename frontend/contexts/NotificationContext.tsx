import React, { createContext, useContext, useState, useEffect, useRef } from 'react';
import * as Notifications from 'expo-notifications';
import * as Device from 'expo-device';
import Constants from 'expo-constants';
import { Platform } from 'react-native';
import AsyncStorage from '@react-native-async-storage/async-storage';
import axios from 'axios';

// Configure notification behavior
Notifications.setNotificationHandler({
  handleNotification: async () => ({
    shouldShowAlert: true,
    shouldPlaySound: true,
    shouldSetBadge: true,
  }),
});

interface Notification {
  id: number;
  title: string;
  body: string;
  type: string;
  data: any;
  read: boolean;
  created_at: string;
}

interface NotificationContextType {
  notifications: Notification[];
  unreadCount: number;
  expoPushToken: string | null;
  registerForPushNotifications: () => Promise<void>;
  fetchNotifications: () => Promise<void>;
  markAsRead: (notificationId: number) => Promise<void>;
  markAllAsRead: () => Promise<void>;
  refreshNotifications: () => Promise<void>;
}

const NotificationContext = createContext<NotificationContextType | undefined>(undefined);

export const NotificationProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [notifications, setNotifications] = useState<Notification[]>([]);
  const [unreadCount, setUnreadCount] = useState(0);
  const [expoPushToken, setExpoPushToken] = useState<string | null>(null);

  const notificationListener = useRef<any>();
  const responseListener = useRef<any>();

  useEffect(() => {
    // Register for push notifications on mount
    registerForPushNotifications();

    // Listen for notifications received while app is in foreground
    notificationListener.current = Notifications.addNotificationReceivedListener(notification => {
      console.log('📱 Notification received:', notification);
      fetchNotifications(); // Refresh notification list
    });

    // Listen for user interactions with notifications
    responseListener.current = Notifications.addNotificationResponseReceivedListener(response => {
      console.log('👆 Notification tapped:', response);
      const data = response.notification.request.content.data;

      // Handle navigation based on notification type
      if (data.type === 'news' && data.news_id) {
        // Navigate to news detail
      } else if (data.type === 'price_alert' && data.symbol) {
        // Navigate to stock detail
      }
    });

    // Initial fetch
    fetchNotifications();

    return () => {
      if (notificationListener.current) {
        notificationListener.current.remove();
      }
      if (responseListener.current) {
        responseListener.current.remove();
      }
    };
  }, []);

  const registerForPushNotifications = async () => {
    try {
      // Check if running in Expo Go
      const isExpoGo = Constants.appOwnership === 'expo';

      if (isExpoGo) {
        console.log('⚠️ Running in Expo Go - Push notifications disabled');
        console.log('💡 In-app notifications will still work!');
        console.log('📱 For push notifications, build a development build with: npx expo run:android');
        return;
      }

      if (!Device.isDevice) {
        console.log('Push notifications only work on physical devices');
        return;
      }

      // Check existing permissions
      const { status: existingStatus } = await Notifications.getPermissionsAsync();
      let finalStatus = existingStatus;

      // Request permission if not granted
      if (existingStatus !== 'granted') {
        const { status } = await Notifications.requestPermissionsAsync();
        finalStatus = status;
      }

      if (finalStatus !== 'granted') {
        console.log('Failed to get push notification permissions');
        return;
      }

      // Get Expo push token
      const projectId = Constants.expoConfig?.extra?.eas?.projectId;
      if (!projectId) {
        console.log('⚠️ No project ID found - skipping push token registration');
        console.log('💡 Run: npx expo login && eas build:configure to set up EAS');
        return;
      }

      const token = await Notifications.getExpoPushTokenAsync({
        projectId,
      });

      console.log('✅ Expo Push Token:', token.data);
      setExpoPushToken(token.data);

      // Save token to backend
      const authToken = await AsyncStorage.getItem('authToken');
      if (authToken) {
        try {
          const backendUrl = process.env.EXPO_PUBLIC_BACKEND_URL || 'http://192.168.31.141:8000';
          await axios.post(
            `${backendUrl}/api/notifications/register-token`,
            { expo_push_token: token.data },
            { headers: { Authorization: `Bearer ${authToken}` } }
          );
          console.log('✅ Push token registered with backend');
        } catch (error) {
          console.error('Failed to register push token:', error);
        }
      }

      // Configure notification channel for Android
      if (Platform.OS === 'android') {
        await Notifications.setNotificationChannelAsync('stock-alerts', {
          name: 'Stock Alerts',
          importance: Notifications.AndroidImportance.MAX,
          vibrationPattern: [0, 250, 250, 250],
          lightColor: '#FF231F7C',
          sound: 'default',
        });
      }
    } catch (error) {
      console.error('Error registering for push notifications:', error);
    }
  };

  const fetchNotifications = async () => {
    try {
      const authToken = await AsyncStorage.getItem('authToken');
      if (!authToken) return;

      const backendUrl = process.env.EXPO_PUBLIC_BACKEND_URL || 'http://192.168.31.141:8000';
      const response = await axios.get(`${backendUrl}/api/notifications`, {
        headers: { Authorization: `Bearer ${authToken}` },
      });

      if (response.data.notifications) {
        setNotifications(response.data.notifications);
        const unread = response.data.notifications.filter((n: Notification) => !n.read).length;
        setUnreadCount(unread);

        // Update app badge
        await Notifications.setBadgeCountAsync(unread);
      }
    } catch (error) {
      console.error('Failed to fetch notifications:', error);
    }
  };

  const markAsRead = async (notificationId: number) => {
    try {
      const authToken = await AsyncStorage.getItem('authToken');
      if (!authToken) return;

      const backendUrl = process.env.EXPO_PUBLIC_BACKEND_URL || 'http://192.168.31.141:8000';
      await axios.post(
        `${backendUrl}/api/notifications/${notificationId}/read`,
        {},
        { headers: { Authorization: `Bearer ${authToken}` } }
      );

      // Update local state
      setNotifications(prev =>
        prev.map(n => (n.id === notificationId ? { ...n, read: true } : n))
      );
      setUnreadCount(prev => Math.max(0, prev - 1));

      // Update badge
      await Notifications.setBadgeCountAsync(Math.max(0, unreadCount - 1));
    } catch (error) {
      console.error('Failed to mark notification as read:', error);
    }
  };

  const markAllAsRead = async () => {
    try {
      const authToken = await AsyncStorage.getItem('authToken');
      if (!authToken) return;

      const backendUrl = process.env.EXPO_PUBLIC_BACKEND_URL || 'http://192.168.31.141:8000';
      // Mark all notifications as read individually
      const unreadNotifications = notifications.filter(n => !n.read);
      await Promise.all(
        unreadNotifications.map(n =>
          axios.post(
            `${backendUrl}/api/notifications/${n.id}/read`,
            {},
            { headers: { Authorization: `Bearer ${authToken}` } }
          )
        )
      );

      // Update local state
      setNotifications(prev => prev.map(n => ({ ...n, read: true })));
      setUnreadCount(0);

      // Clear badge
      await Notifications.setBadgeCountAsync(0);
    } catch (error) {
      console.error('Failed to mark all as read:', error);
    }
  };

  const refreshNotifications = async () => {
    await fetchNotifications();
  };

  return (
    <NotificationContext.Provider
      value={{
        notifications,
        unreadCount,
        expoPushToken,
        registerForPushNotifications,
        fetchNotifications,
        markAsRead,
        markAllAsRead,
        refreshNotifications,
      }}
    >
      {children}
    </NotificationContext.Provider>
  );
};

export const useNotifications = () => {
  const context = useContext(NotificationContext);
  if (!context) {
    throw new Error('useNotifications must be used within NotificationProvider');
  }
  return context;
};
