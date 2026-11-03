import {
  ActionIcon, Button, Checkbox, Chip, Group, NumberInput, SegmentedControl, Select, Stack, TagsInput, Text, TextInput,
} from '@mantine/core';
import { IconPlus, IconTrash } from '@tabler/icons-react';

// The same constraint IR the model fills in, as a form. Editing here and
// describing changes in words produce the identical object.

export const EMPTY_IR = {
  origin: null, destination: null, returnToOrigin: false, stops: [], stopOrder: 'as_listed', arriveBy: null,
  avoid: { highways: false, tolls: false, ferries: false, storms: false, areas: [], roads: [] },
  meetup: { friendFrom: null, friendLeftMinutesAgo: null, what: null, maxDetourMinutes: null },
  team: { partner: null, partnerAt: null, tasks: [] },
  useRoads: [], custom: null,
  profile: 'auto', clarification: null,
};

const NEW_STOP = {
  kind: 'errand', text: null, what: '', brands: [], side: 'any', maxDetourMinutes: null,
  position: 'anywhere', dwellMinutes: null, closesAt: null,
};

const blank = (v) => (v === '' || v === undefined ? null : v);

const Row = ({ children }) => <Group gap={6} wrap="nowrap" align="flex-end">{children}</Group>;

const ConstraintEditor = ({ ir, onChange }) => {
  const set = (patch) => onChange({ ...ir, ...patch });
  const setAvoid = (patch) => set({ avoid: { ...ir.avoid, ...patch } });
  const setStop = (i, patch) => set({ stops: ir.stops.map((s, j) => (j === i ? { ...s, ...patch } : s)) });
  const setMeet = (patch) => set({ meetup: { ...ir.meetup, ...patch } });
  const toggles = ['highways', 'tolls', 'ferries', 'storms'].filter((k) => ir.avoid[k]);

  return (
    <Stack gap={10} className="constraint-editor">
      <TextInput size="xs" label="From" placeholder="Current location" value={ir.origin || ''}
        onChange={(e) => set({ origin: blank(e.currentTarget.value) })} />

      <div>
        <Text size="xs" fw={600} mb={4}>Stops</Text>
        <Stack gap={8}>
          {ir.stops.map((stop, i) => (
            <div className="stop-editor" key={i}>
              <Row>
                <SegmentedControl size="xs" value={stop.kind}
                  data={[{ value: 'errand', label: 'Errand' }, { value: 'place', label: 'Place' }]}
                  onChange={(kind) => setStop(i, kind === 'place'
                    ? { kind, text: stop.what || stop.text || '', what: null, brands: [], side: 'any' }
                    : { kind, what: stop.text || stop.what || '', text: null })} />
                <TextInput size="xs" style={{ flex: 1 }}
                  placeholder={stop.kind === 'place' ? 'Place, e.g. Tartine Bakery' : 'What, e.g. coffee, gas'}
                  value={(stop.kind === 'place' ? stop.text : stop.what) || ''}
                  onChange={(e) => setStop(i, stop.kind === 'place'
                    ? { text: e.currentTarget.value } : { what: e.currentTarget.value.toLowerCase() })} />
                <ActionIcon variant="subtle" color="red" onClick={() => set({ stops: ir.stops.filter((_, j) => j !== i) })}>
                  <IconTrash size={14} />
                </ActionIcon>
              </Row>
              {stop.kind === 'errand' && (
                <Group gap={8} mt={6} wrap="wrap" align="flex-end">
                  <TagsInput size="xs" style={{ flex: 1, minWidth: 150 }} placeholder="Brands (any)"
                    value={stop.brands || []} onChange={(brands) => setStop(i, { brands })} />
                  <NumberInput size="xs" w={92} placeholder="Max detour" suffix=" min" min={0} max={120}
                    value={stop.maxDetourMinutes ?? ''} onChange={(v) => setStop(i, { maxDetourMinutes: v === '' ? null : Number(v) })} />
                  <Select size="xs" w={100} value={stop.position} allowDeselect={false}
                    data={['anywhere', 'early', 'middle', 'late']} onChange={(position) => setStop(i, { position })} />
                  <Checkbox size="xs" label="On the right" checked={stop.side === 'right'}
                    onChange={(e) => setStop(i, { side: e.currentTarget.checked ? 'right' : 'any' })} />
                </Group>
              )}
            </div>
          ))}
          <Group gap={8}>
            <Button size="compact-xs" variant="light" leftSection={<IconPlus size={12} />}
              onClick={() => set({ stops: [...ir.stops, { ...NEW_STOP }] })}>Add stop</Button>
            {ir.stops.length > 1 && (
              <Checkbox size="xs" label="Optimize order" checked={ir.stopOrder === 'any'}
                onChange={(e) => set({ stopOrder: e.currentTarget.checked ? 'any' : 'as_listed' })} />
            )}
          </Group>
        </Stack>
      </div>

      <Group gap={8} grow>
        <TextInput size="xs" label="Arrive by" placeholder="HH:MM" value={ir.arriveBy || ''}
          onChange={(e) => set({ arriveBy: blank(e.currentTarget.value) })} />
        <Select size="xs" label="Vehicle" value={ir.profile} allowDeselect={false}
          data={['auto', 'truck', 'motorcycle', 'bicycle', 'pedestrian']} onChange={(profile) => set({ profile })} />
      </Group>

      <div>
        <Text size="xs" fw={600} mb={4}>Avoid</Text>
        <Chip.Group multiple value={toggles}
          onChange={(on) => setAvoid(Object.fromEntries(['highways', 'tolls', 'ferries', 'storms'].map((k) => [k, on.includes(k)])))}>
          <Group gap={6}>
            {['highways', 'tolls', 'ferries', 'storms'].map((k) => <Chip key={k} size="xs" value={k}>{k}</Chip>)}
          </Group>
        </Chip.Group>
        <Stack gap={6} mt={8}>
          {ir.avoid.areas.map((area, i) => (
            <Row key={`a${i}`}>
              <TextInput size="xs" style={{ flex: 1 }} placeholder="Area, e.g. downtown San Francisco" value={area.text}
                onChange={(e) => setAvoid({ areas: ir.avoid.areas.map((a, j) => (j === i ? { ...a, text: e.currentTarget.value } : a)) })} />
              <Select size="xs" w={112} value={area.roads} allowDeselect={false}
                data={[{ value: 'freeways', label: 'its freeways' }, { value: 'all', label: 'all of it' }]}
                onChange={(roads) => setAvoid({ areas: ir.avoid.areas.map((a, j) => (j === i ? { ...a, roads } : a)) })} />
              <ActionIcon variant="subtle" color="red" onClick={() => setAvoid({ areas: ir.avoid.areas.filter((_, j) => j !== i) })}>
                <IconTrash size={14} />
              </ActionIcon>
            </Row>
          ))}
          {ir.avoid.roads.map((road, i) => (
            <Row key={`r${i}`}>
              <TextInput size="xs" style={{ flex: 1 }} placeholder="Road or bridge, e.g. Bay Bridge" value={road.name}
                onChange={(e) => setAvoid({ roads: ir.avoid.roads.map((r, j) => (j === i ? { ...r, name: e.currentTarget.value } : r)) })} />
              <ActionIcon variant="subtle" color="red" onClick={() => setAvoid({ roads: ir.avoid.roads.filter((_, j) => j !== i) })}>
                <IconTrash size={14} />
              </ActionIcon>
            </Row>
          ))}
          <Group gap={8}>
            <Button size="compact-xs" variant="light" leftSection={<IconPlus size={12} />}
              onClick={() => setAvoid({ areas: [...ir.avoid.areas, { text: '', radiusMiles: null, roads: 'freeways' }] })}>Area</Button>
            <Button size="compact-xs" variant="light" leftSection={<IconPlus size={12} />}
              onClick={() => setAvoid({ roads: [...ir.avoid.roads, { name: '', near: null }] })}>Road</Button>
          </Group>
        </Stack>
      </div>

      <div>
        <Text size="xs" fw={600} mb={4}>Meet a friend</Text>
        <Group gap={8} grow>
          <TextInput size="xs" placeholder="Coming from, e.g. Davis" value={ir.meetup.friendFrom || ''}
            onChange={(e) => setMeet({ friendFrom: blank(e.currentTarget.value), friendLeftMinutesAgo: ir.meetup.friendLeftMinutesAgo ?? 0 })} />
          <TextInput size="xs" placeholder="Meet at (coffee)" value={ir.meetup.what || ''}
            onChange={(e) => setMeet({ what: blank(e.currentTarget.value.toLowerCase()) })} />
          <NumberInput size="xs" placeholder="Max detour" suffix=" min" min={0} value={ir.meetup.maxDetourMinutes ?? ''}
            onChange={(v) => setMeet({ maxDetourMinutes: v === '' ? null : Number(v) })} />
        </Group>
      </div>
    </Stack>
  );
};

export default ConstraintEditor;
