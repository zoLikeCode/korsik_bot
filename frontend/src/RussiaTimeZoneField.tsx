import { Field } from "./ui";
export const RUSSIA_TIME_ZONES = [
  { value: "Europe/Kaliningrad", label: "Калининград", offset: "+02:00" },
  { value: "Europe/Moscow", label: "Москва", offset: "+03:00" },
  { value: "Europe/Kirov", label: "Киров", offset: "+03:00" },
  { value: "Europe/Volgograd", label: "Волгоград", offset: "+03:00" },
  { value: "Europe/Astrakhan", label: "Астрахань", offset: "+04:00" },
  { value: "Europe/Saratov", label: "Саратов", offset: "+04:00" },
  { value: "Europe/Ulyanovsk", label: "Ульяновск", offset: "+04:00" },
  { value: "Europe/Samara", label: "Самара", offset: "+04:00" },
  { value: "Asia/Yekaterinburg", label: "Екатеринбург", offset: "+05:00" },
  { value: "Asia/Omsk", label: "Омск", offset: "+06:00" },
  { value: "Asia/Novosibirsk", label: "Новосибирск", offset: "+07:00" },
  { value: "Asia/Barnaul", label: "Барнаул", offset: "+07:00" },
  { value: "Asia/Tomsk", label: "Томск", offset: "+07:00" },
  { value: "Asia/Novokuznetsk", label: "Новокузнецк", offset: "+07:00" },
  { value: "Asia/Krasnoyarsk", label: "Красноярск", offset: "+07:00" },
  { value: "Asia/Irkutsk", label: "Иркутск", offset: "+08:00" },
  { value: "Asia/Chita", label: "Чита", offset: "+09:00" },
  { value: "Asia/Yakutsk", label: "Якутск", offset: "+09:00" },
  { value: "Asia/Khandyga", label: "Хандыга", offset: "+09:00" },
  { value: "Asia/Ust-Nera", label: "Усть-Нера", offset: "+10:00" },
  { value: "Asia/Vladivostok", label: "Владивосток", offset: "+10:00" },
  { value: "Asia/Sakhalin", label: "Южно-Сахалинск", offset: "+11:00" },
  { value: "Asia/Magadan", label: "Магадан", offset: "+11:00" },
  { value: "Asia/Srednekolymsk", label: "Среднеколымск", offset: "+11:00" },
  {
    value: "Asia/Kamchatka",
    label: "Петропавловск-Камчатский",
    offset: "+12:00",
  },
  { value: "Asia/Anadyr", label: "Анадырь", offset: "+12:00" },
] as const;
export function russiaTimeZoneLabel(value?: string) {
  const zone = RUSSIA_TIME_ZONES.find((candidate) => candidate.value === value);
  return zone ? `UTC${zone.offset} — ${zone.label}` : value || "—";
}
export default function RussiaTimeZoneField({
  value,
  onChange,
  disabled = false,
}: {
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
}) {
  return (
    <Field label="Часовой пояс">
      <select
        required
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value)}
      >
        {RUSSIA_TIME_ZONES.map((zone) => (
          <option key={zone.value} value={zone.value}>
            UTC{zone.offset} — {zone.label}
          </option>
        ))}
      </select>
    </Field>
  );
}
