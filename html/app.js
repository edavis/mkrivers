"use strict";

function getFavicon(url) {
    var a = document.createElement('a');
    a.href = url;
    return ("http://www.google.com/s2/favicons?domain=" + a.hostname);
}

var RiverList = React.createClass({
    fetchRiver: function() {
        // Skip update if user has scrolled down any. This prevents
        // the river from jumping around with new updates as you try
        // to read it.
        if (window.scrollY > 0) {
            return;
        }

        $.ajax({
            url: this.props.url,
            dataType: 'jsonp',
            jsonp: false,
            jsonpCallback: 'onGetRiverStream',
            success: function(data) {
                console.log(this.props.url, 'success');
                this.setState({feeds: data.updatedFeeds.updatedFeed});
            }.bind(this),
            error: function(xhr, status, err) {
                console.error(this.props.url, status, err.toString());
            }.bind(this)
        });
    },
    getInitialState: function() {
        return {feeds: []};
    },
    componentDidMount: function() {
        this.fetchRiver();
        setInterval(this.fetchRiver, this.props.pollInterval);
    },
    render: function() {
        var feeds = this.state.feeds.map(function(feed) {
            return (
                <RiverFeed key={feed.whenLastUpdate + feed.feedUrl} feed={feed} />
            );
        });
        return (
            <div className="riverList">
                {feeds}
            </div>
        );
    }
});

var RiverFeed = React.createClass({
    render: function() {
        var items = this.props.feed.item.map(function(item) {
            return (
                <RiverItem key={item.id} item={item} />
            );
        });
        var whenLastUpdate = moment(this.props.feed.whenLastUpdate, 'ddd, DD MMMM YYYY HH:mm:ss ZZ').format('h:mm A; DD MMM');
        var favicon = getFavicon(this.props.feed.websiteUrl);
        return (
            <div className="riverFeed">
                <div className="riverHeader">
                    <div className="updateInfo">
                        {whenLastUpdate}
                    </div>
                    <div className="feedInfo">
                        <img className="favicon" src={favicon}></img>
                        <a className="feedTitle" href={this.props.feed.websiteUrl}>{this.props.feed.feedTitle}</a>&nbsp;
                        <a className="feedUrl" href={this.props.feed.feedUrl}>(Feed)</a>
                    </div>
                </div>
                <div className="riverItems">
                    {items}
                </div>
            </div>
        );
    }
});

var RiverItem = React.createClass({
    render: function() {
        var whenAgo = moment(this.props.item.pubDate, 'ddd, DD MMMM YYYY HH:mm:ss ZZ').fromNow();
        return (
            <div className="riverItem">
                <div className="itemTitle"><a target="_blank" href={this.props.item.link}>{this.props.item.title}</a></div>
                <div className="itemBody">{this.props.item.body}</div>
                <div className="itemMeta">
                    <span className="whenAgo">{whenAgo}</span>
                    {this.props.item.comments && <span className="commentsUrl">&nbsp;&bull;&nbsp;<a target="_blank" href={this.props.item.comments}>Comments</a></span>}
                </div>
            </div>
        );
    }
});

ReactDOM.render(
    <RiverList url={RiverConfig.url} pollInterval={RiverConfig.poll * 1000} />,
    document.getElementById(RiverConfig.mount)
);
