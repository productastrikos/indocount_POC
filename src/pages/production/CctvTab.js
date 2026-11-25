import React from 'react';
import { Card } from '../../components/ui';
import CctvWall from '../../components/cctv/CctvWall';

const suffix = (f) => f.camera.split('-').pop();
const CHIPS = [
  { key: 'all', label: 'All production cameras' },
  { key: 'fibre', label: 'Fibre opening & carding', test: (f) => ['07', '08'].includes(suffix(f)) },
  { key: 'filling', label: 'Filling', test: (f) => suffix(f) === '01' },
  { key: 'sewing', label: 'Sewing', test: (f) => suffix(f) === '02' },
  { key: 'compression', label: 'Compression & bagging', test: (f) => suffix(f) === '03' },
  { key: 'packing', label: 'Carton packing', test: (f) => ['04', '05'].includes(suffix(f)) },
];

/** Production → CCTV · Vision AI: every line camera with live CV overlays. */
export default function CctvTab({ factory }) {
  return (
    <Card title="CCTV · Vision AI" padded
      subtitle="Line cameras with computer-vision overlays: workers tracked and identified by RFID badge, activity, pillows counted into cartons and across the line, carton labels read">
      <CctvWall factory={factory} filter={(f) => suffix(f) !== '06'} chips={CHIPS} cols={3} />
    </Card>
  );
}
