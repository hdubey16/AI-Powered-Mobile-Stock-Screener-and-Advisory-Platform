import { useColorScheme as useRNColorScheme } from 'react-native';
import { Colors } from '../constants/Colors';

export function useColor<K extends keyof typeof Colors.light & keyof typeof Colors.dark>(
  colorName: K,
  props?: { light?: string; dark?: string }
): (typeof Colors.light)[K] | string {
  const theme = useRNColorScheme() ?? 'light';
  const colorFromProps = props?.[theme];

  if (colorFromProps) {
    return colorFromProps;
  } else {
    return Colors[theme][colorName];
  }
}
