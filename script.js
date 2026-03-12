// Get the date list and match list containers
const dateList = document.getElementById('dateList');
const matchList = document.getElementById('matchList');

// Helper function to format date to "DD MMM" format
function formatDate(date) {
    const options = { day: '2-digit', month: 'short' };
    return date.toLocaleDateString('en-GB', options).toUpperCase();
}

// Generate dates for the date navigation
function generateDates() {
    const today = new Date();
    const dates = [];

    // Generate dates: two days before, today, and two days after
    for (let i = -2; i <= 2; i++) {
        const date = new Date(today);
        date.setDate(today.getDate() + i);
        dates.push({
            day: date.toLocaleDateString('en-GB', { weekday: 'short' }).toUpperCase(),
            date: formatDate(date),
            isToday: i === 0
        });
    }

    // Render dates in the navigation bar
    dateList.innerHTML = '';
    dates.forEach(dateObj => {
        const dateItem = document.createElement('div');
        dateItem.classList.add('date-item');
        if (dateObj.isToday) dateItem.classList.add('today');

        const dayElem = document.createElement('div');
        dayElem.textContent = dateObj.day;

        const dateElem = document.createElement('div');
        dateElem.textContent = dateObj.date;

        dateItem.appendChild(dayElem);
        dateItem.appendChild(dateElem);
        dateList.appendChild(dateItem);
    });
}

// Generate nine football matches
function generateMatches() {
    const matches = [
        { time: "20:30", home: "RB Leipzig", away: "Borussia Monchengladbach", tip: "1" },
        { time: "17:30", home: "FC St. Pauli", away: "Bayern Munich", tip: "2" },
        { time: "17:30", home: "FSV Mainz 05", away: "Borussia Dortmund", tip: "2" },
        { time: "20:30", home: "Hamburg SV", away: "Schalke 04", tip: "1" },
        { time: "15:30", home: "Werder Bremen", away: "Eintracht Frankfurt", tip: "1" },
        { time: "18:00", home: "Hannover 96", away: "SC Freiburg", tip: "2" },
        { time: "14:00", home: "Stuttgart", away: "Hoffenheim", tip: "1" },
        { time: "21:00", home: "VfL Wolfsburg", away: "Union Berlin", tip: "X" },
        { time: "19:30", home: "Cologne", away: "Hertha BSC", tip: "2" }
    ];

    // Clear the match list and render each match
    matchList.innerHTML = '';
    matches.forEach(match => {
        const matchItem = document.createElement('div');
        matchItem.classList.add('match-item');

        const timeElem = document.createElement('div');
        timeElem.classList.add('time');
        timeElem.textContent = match.time;

        const teamElem = document.createElement('div');
        teamElem.classList.add('team');
        teamElem.textContent = `${match.home} - ${match.away}`;

        const tipElem = document.createElement('div');
        tipElem.classList.add('tip');
        tipElem.textContent = match.tip;

        matchItem.appendChild(timeElem);
        matchItem.appendChild(teamElem);
        matchItem.appendChild(tipElem);
        matchList.appendChild(matchItem);
    });
}

// Initialize the date navigation and matches on page load
generateDates();
generateMatches();
