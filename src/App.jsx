import { useEffect, useState } from 'react';
import { MantineProvider, createTheme } from '@mantine/core';
import '@mantine/core/styles.css';
import DriveView from './DriveView';
import PlannerApp, { apiUrl } from './PlannerApp';
import TurnByTurnNavigation from './TurnByTurnNavigation';
import './App.css';

const theme = createTheme({
  primaryColor: 'blue',
  fontFamily: 'Inter, "Segoe UI", sans-serif',
  headings: {
    fontFamily: 'Inter, "Segoe UI", sans-serif',
  },
  defaultRadius: 'md',
});

const useHash = () => {
  const [hash, setHash] = useState(window.location.hash);
  useEffect(() => {
    const onChange = () => setHash(window.location.hash);
    window.addEventListener('hashchange', onChange);
    return () => window.removeEventListener('hashchange', onChange);
  }, []);
  return hash;
};

// A trip handed off through a share link (/share.html -> "/?t=<token>&drive=trip"):
// rebuild the plan from its navRequest and go straight to navigation.
const SHARE_PARAMS = new URLSearchParams(window.location.search);
const SHARE_QUERY = SHARE_PARAMS.get('t') ? `t=${encodeURIComponent(SHARE_PARAMS.get('t'))}` : '';

const planFromTrip = (trip) => {
  const points = trip.navRequest.points;
  return {
    origin: points[0],
    stops: points.slice(1, -1),
    destination: points[points.length - 1],
    navRequest: trip.navRequest,
  };
};

function App() {
  const hash = useHash();
  const [drive, setDrive] = useState(null);
  const [shareError, setShareError] = useState('');
  useEffect(() => {
    if (!SHARE_QUERY || SHARE_PARAMS.get('drive') !== 'trip') return;
    fetch(apiUrl(`/api/trip/active?${SHARE_QUERY}`)).then((r) => r.json())
      .then((data) => {
        if (data.trip) setDrive(planFromTrip(data.trip));
        else setShareError(data.error || 'No trip has been sent to you yet.');
      })
      .catch((err) => setShareError(err.message));
  }, []);
  return (
    <MantineProvider theme={theme}>
      {hash === '#legacy' ? (
        <main className="turn-by-turn-app">
          <TurnByTurnNavigation />
        </main>
      ) : (
        <>
          <div style={{ display: drive ? 'none' : 'block' }}>
            <PlannerApp onNavigate={setDrive} />
          </div>
          {shareError && <div className="share-error">{shareError}</div>}
          {drive && <DriveView plan={drive} apiUrl={apiUrl} shareQuery={SHARE_QUERY} onExit={() => setDrive(null)} />}
        </>
      )}
    </MantineProvider>
  );
}

export default App;
