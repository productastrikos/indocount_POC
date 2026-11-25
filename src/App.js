import React from 'react';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { ThemeProvider } from './theme/ThemeProvider';
import { DataProvider, useApp } from './data/DataProvider';
import Layout from './components/layout/Layout';
import Login from './pages/Login';
import { Loading } from './components/common';
import { ROUTES } from './routes';

function Shell() {
  const { user, authChecked, meta } = useApp();
  if (!authChecked) return <div className="p-10"><Loading /></div>;
  if (!user) return <Login />;
  if (!meta) return <div className="p-10"><Loading rows={5} /></div>;
  return (
    <Layout>
      <Routes>
        {ROUTES.map((r) => <Route key={r.path} path={r.path} element={r.element} />)}
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Layout>
  );
}

export default function App() {
  return (
    <ThemeProvider>
      <DataProvider>
        <BrowserRouter>
          <Shell />
        </BrowserRouter>
      </DataProvider>
    </ThemeProvider>
  );
}
