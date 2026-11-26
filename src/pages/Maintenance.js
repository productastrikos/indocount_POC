import React, { useState } from 'react';
import { Tabs } from '../components/ui';
import { PageTitle } from '../components/common';
import { useApp } from '../data/DataProvider';
import MachinesTab, { MachineDrawer } from './production/MachinesTab';
import PredictiveTab from './production/PredictiveTab';

const TABS = [
  { value: 'predictive', label: 'Failure prediction & plan' },
  { value: 'health', label: 'Machine health & work orders' },
];

/**
 * Predictive Maintenance module: the reliability view of every production machine in the
 * selected plant(s). The same views are also reachable as Production tabs; this page gives
 * maintenance planners their own entry point.
 */
export default function Maintenance() {
  const { filters } = useApp();
  const [tab, setTab] = useState('predictive');
  const [machineId, setMachineId] = useState(null);
  const factory = filters.factory;
  return (
    <div className="space-y-4">
      <PageTitle title="Predictive Maintenance"
        subtitle="Failure probability, remaining useful life and the cheapest repair window for every machine — fibre openers, flappers, Garnett and blow-line motors, filling heads">
      </PageTitle>
      <Tabs tabs={TABS} value={tab} onChange={setTab} variant="underline" />
      {tab === 'predictive' && <PredictiveTab factory={factory} onOpenMachine={setMachineId} />}
      {tab === 'health' && <MachinesTab factory={factory} onOpen={setMachineId} />}
      {machineId && <MachineDrawer id={machineId} onClose={() => setMachineId(null)} />}
    </div>
  );
}
